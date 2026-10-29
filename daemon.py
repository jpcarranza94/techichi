#!/usr/bin/env python3
"""The daemon: one pass applies the Router to every pending Event, keeps the helper Sessions, and syncs herdr.
See DESIGN.md sections "Daemon", "Router" and "Delivery". The Router itself is router.py."""
import json
import socket
import subprocess
import sys
import time
from datetime import timedelta

sys.dont_write_bytecode = True  # the daemon writes only in the config folder: no __pycache__ next to the code
import config  # noqa: E402
import factory  # noqa: E402
import github_source  # noqa: E402
import herdr  # noqa: E402
import jira_source  # noqa: E402
import registry  # noqa: E402
from router import admit, route, wrapper  # noqa: E402,F401  (route and admit: daemon.route stays for callers)

NOW = registry.NOW
# pane.updated carries agent_status and agent_session for every pane. pane.agent_status_changed needs one
# subscription per pane, so the daemon does not use it.
TYPES = ["pane.updated", "pane.agent_detected", "pane.closed", "pane.exited", "workspace.closed",
         "worktree.created", "worktree.removed"]
TICK = 30  # seconds between passes when herdr is quiet
RETRY = timedelta(minutes=5)  # a failed helper start or planner notice waits this long


def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, flush=True)


# --- Actions --------------------------------------------------------------------------------------------

def prompt_action(pane, text):
    herdr.prompt(pane, text)


def spawn_action(key, slug=None):
    import spawn  # the spawner shells out to herdr, git and gh by absolute path
    return spawn.spawn(key, slug or None)["session_id"]


def planner_action(text):
    import spawn
    return spawn.spawn_planner(text)


def relay_action(text):
    import spawn
    return spawn.spawn_relay(text)


def existing_work(e):
    return type(e).__name__ == "ExistingWork"  # spawn.py raises it. No import here, so tests need no git.


# --- One pass -------------------------------------------------------------------------------------------

def process(con, pol, prompt=None, spawn=None, start_planner=None):
    """One Router pass over every pending Event. Batches all deliveries for one Session into one prompt.
    Spawns at most one ticket per pass, gap_seconds after the last spawn."""
    # Resolved at call time, so a test can replace the module Actions once.
    prompt, spawn, start_planner = prompt or prompt_action, spawn or spawn_action, start_planner or planner_action
    # ponytail: full scan of pending Events on each pass, fine for hundreds. Index on status already exists.
    # Plan position first, then the oldest, so the next spawn is the planner's first choice.
    evs = con.execute("SELECT e.* FROM events e LEFT JOIN plan p ON e.kind = 'ticket' AND p.ticket = e.ref "
                      "WHERE e.status IN ('queued','held','unrouted') "
                      "ORDER BY p.position IS NULL, p.position, e.created_at").fetchall()
    batches, ready, attention = {}, [], []
    for ev in evs:
        status, sid, reason = route(con, ev, pol)
        if status == "spawn":
            ready.append(ev)
            continue
        if status == "delivered":
            batches.setdefault(sid, []).append(ev)
            continue
        kind = (reason or "").split(":")[0]
        if kind in ("needs-plan", "needs-decision") and (ev["reason"] or "").split(":")[0] != kind:
            attention.append(f"{ev['ref']} ({kind})")  # once per Event: only on the change to this reason
        if (status, sid, reason) != (ev["status"], ev["session_id"], ev["reason"]):
            con.execute("UPDATE events SET status = ?, session_id = ?, reason = ? WHERE id = ?",
                        (status, sid, reason, ev["id"]))
    if ready and registry.due(con, "spawn:last", timedelta(seconds=pol["spawn"]["gap_seconds"])):
        # Simultaneous Claude starts can race on Claude's own config files. One start per pass, then the gap.
        ev = ready[0]
        registry.set_cursor(con, "spawn:last", registry.now())
        try:
            sid = spawn(ev["ref"], json.loads(ev["payload"]).get("slug"))
            log("spawned", ev["ref"], sid)
            # The kickoff prompt covers the ticket. A second prompt only costs the worker a turn.
            con.execute(f"UPDATE events SET status = 'delivered', session_id = ?, reason = 'first-prompt', "
                        f"delivered_at = {NOW} WHERE id = ?", (sid, ev["id"]))
        except Exception as e:
            if existing_work(e):
                con.execute("UPDATE events SET reason = ? WHERE id = ?", (f"needs-decision: {e}"[:500], ev["id"]))
                attention.append(f"{ev['ref']} (needs-decision: {e})")
            else:
                con.execute("UPDATE events SET status = 'unrouted', reason = ? WHERE id = ?",
                            (f"spawn failed: {e}"[:500], ev["id"]))
                attention.append(f"{ev['ref']} (spawn failed)")
            log("spawn stopped", ev["ref"], e)
    for sid, group in batches.items():
        pane = con.execute("SELECT pane_id FROM sessions WHERE session_id = ?", (sid,)).fetchone()["pane_id"]
        ids = [e["id"] for e in group]
        q = ",".join("?" * len(ids))
        try:
            prompt(pane, wrapper(con, sid, group, pol))
        except Exception as e:
            con.execute(f"UPDATE events SET status = 'held', session_id = ?, reason = ? WHERE id IN ({q})",
                        [sid, f"prompt failed: {e}"[:500]] + ids)
            log("prompt failed", sid, e)
            continue
        con.execute(f"UPDATE events SET status = 'delivered', session_id = ?, reason = NULL, delivered_at = {NOW} "
                    f"WHERE id IN ({q})", [sid] + ids)
        log("delivered", len(ids), "to", sid)
    planner(con, pol, prompt, start_planner, attention)


# --- Helper Sessions: the planner and the relay -----------------------------------------------------------

def end_stuck(con, role):
    """A helper whose start failed stays registered. After 10 minutes it is not starting, it is dead."""
    for r in con.execute("SELECT session_id FROM sessions WHERE role = ? AND state = 'registered' "
                         "AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-10 minutes')", (role,)).fetchall():
        con.execute(f"UPDATE sessions SET state = 'ended', ended_at = {NOW} WHERE session_id = ?", (r["session_id"],))
        log(role, r["session_id"], "registered for over 10 min, set to ended")


def relay(con, pol, prompt=None, start=None):
    """Keep one relay session, and prompt it with "[factory] slack poll" while it is idle or done.
    The interval is poll_minutes, and it doubles up to max_poll_minutes while Slack is quiet."""
    if "slack" not in pol:
        return
    prompt, start, slack = prompt or prompt_action, start or relay_action, pol["slack"]
    end_stuck(con, "relay")
    live = con.execute("SELECT pane_id, agent_status FROM sessions WHERE role = 'relay' AND state = 'live' "
                       "ORDER BY last_seen DESC").fetchone()
    every = timedelta(minutes=slack["poll_minutes"])
    if not live:
        starting = con.execute("SELECT 1 FROM sessions WHERE role = 'relay' AND state = 'registered'").fetchone()
        if starting or not registry.due(con, "relay:next"):
            return  # at most one relay. A failed start waits RETRY.
        try:
            start("[factory] slack poll")  # the first prompt is the first poll
            registry.wait(con, "slack:poll_next", every)
            log("relay started")
        except Exception as e:
            registry.wait(con, "relay:next", RETRY)
            log("relay start failed, retry in 5 min:", str(e)[:300])
        return
    if not registry.due(con, "slack:poll_next"):
        return
    # Quiet Slack: the interval doubles after each tick that did not move the Slack cursor, up to max_poll_minutes.
    c = registry.cursor
    seen = c(con, "slack") or ""
    quiet = min(int(c(con, "slack:quiet") or 0) + 1, 10) if seen == c(con, "slack:seen") else 0
    registry.set_cursor(con, "slack:seen", seen)
    registry.set_cursor(con, "slack:quiet", str(quiet))
    registry.wait(con, "slack:poll_next", min(every * 2 ** quiet, timedelta(minutes=slack["max_poll_minutes"])))
    if live["agent_status"] in ("idle", "done"):
        try:
            prompt(live["pane_id"], "[factory] slack poll")
        except Exception as e:
            log("relay poll prompt failed:", str(e)[:300])
    elif live["agent_status"] == "blocked":
        log("relay blocked, slack poll skipped")
    # working: the last poll still runs. Skip this tick.


def planner(con, pol, prompt, start, new):
    """Tell the planner about tickets that need a plan or a decision. Start one when none is live.
    The pending list lives in cursors, so a failed notice goes out on a later pass."""
    pending = json.loads(registry.cursor(con, "planner:pending") or "[]")
    pending = list(dict.fromkeys(pending + new))
    if not pending:
        return
    registry.set_cursor(con, "planner:pending", json.dumps(pending))
    if not registry.due(con, "planner:next"):
        return
    live = con.execute("SELECT pane_id FROM sessions WHERE role = 'planner' AND state = 'live' "
                       "ORDER BY last_seen DESC").fetchone()
    end_stuck(con, "planner")
    starting = con.execute("SELECT 1 FROM sessions WHERE role = 'planner' AND state = 'registered'").fetchone()
    if not live and starting:
        return  # at most one planner. The one that starts gets the list when it is live.
    n = len({x.split(" ")[0] for x in pending})
    text = (f"[factory] {n} {'ticket needs' if n == 1 else 'tickets need'} a plan or a decision: {', '.join(pending)}. "
            f"Run plan.py show. Handle it per the {pol['skills']['planner']} skill.")
    try:
        if live:
            prompt(live["pane_id"], text)
        else:
            start(text)
    except Exception as e:
        # ponytail: a fixed retry. A blocked planner or a failed start does not log on every pass.
        registry.wait(con, "planner:next", RETRY)
        log("planner notice failed, retry in 5 min:", str(e)[:300])
        return
    registry.set_cursor(con, "planner:pending", "[]")
    log("planner", "prompted" if live else "started", ":", ", ".join(pending))


paged = set()


def page_old_holds(con, pol):
    # ponytail: log only, once per Event per daemon run. A held Event almost always means a blocked pane,
    # and a herdr notifier already pages the operator for that. Add paging if a hold outlives the block.
    mins = pol["hold"]["notify_after_minutes"]
    for r in con.execute("SELECT id, session_id, reason FROM events WHERE status = 'held' "
                         f"AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-{int(mins)} minutes')"):
        if r["id"] not in paged:
            paged.add(r["id"])
            log(f"held over {mins} min:", r["id"], r["session_id"], r["reason"])


# --- Sync from herdr ------------------------------------------------------------------------------------

def branch_of(cwd):
    try:
        return subprocess.run([config.GIT, "-C", cwd, "branch", "--show-current"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def sync(con, snap):
    """Match herdr agents to Sessions by UUID. Missing Sessions go gone. Unknown agents become unclaimed workers."""
    seen = set()
    for a in snap["agents"]:
        sid = herdr.session_id(a) if a.get("agent") == "claude" else None
        if not sid:
            continue
        seen.add(sid)
        old = con.execute("SELECT agent_status, state, cwd FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        status, cwd = a.get("agent_status"), a.get("cwd")
        if not old:
            con.execute("INSERT INTO sessions (session_id, role, project, state) VALUES (?, 'worker', ?, 'live')",
                        (sid, registry.project_for_cwd(cwd or ".")))
            log("unclaimed agent", sid, a.get("pane_id"))
        changed = not old or old["agent_status"] != status or old["state"] != "live" or old["cwd"] != cwd
        con.execute(f"UPDATE sessions SET state = CASE state WHEN 'ended' THEN state ELSE 'live' END, pane_id = ?, "
                    f"workspace_id = ?, agent_status = ?, cwd = ?, last_seen = {NOW} WHERE session_id = ?",
                    (a["pane_id"], a.get("workspace_id"), status, cwd, sid))
        if changed and cwd:
            con.execute("UPDATE sessions SET branch = coalesce(?, branch) WHERE session_id = ?", (branch_of(cwd), sid))
    rows = con.execute("SELECT session_id FROM sessions WHERE state IN ('live','registered')").fetchall()
    for r in rows:
        # A registered Session that herdr does not show yet stays registered. The spawner may still start it.
        if r["session_id"] not in seen:
            con.execute("UPDATE sessions SET state = 'gone' WHERE session_id = ? AND state = 'live'", (r["session_id"],))


def apply_tokens(con):
    """Pane tokens do not survive a herdr restart. Re-apply ticket=KEY on each live pane."""
    for r in con.execute("SELECT s.pane_id, min(l.ref) AS ref FROM sessions s JOIN links l USING (session_id) "
                         "WHERE s.state = 'live' AND l.kind = 'ticket' GROUP BY s.session_id"):
        try:
            herdr.report_ticket(r["pane_id"], r["ref"])
        except Exception as e:
            log("token failed", r["pane_id"], e)


def resync(con, pol, tokens=False):
    gone = {r[0] for r in con.execute("SELECT session_id FROM sessions WHERE state = 'gone'")}
    sync(con, herdr.snapshot())
    if tokens:
        apply_tokens(con)
    for name, source in (("github", github_source), ("jira", jira_source)):
        try:
            n = source.poll(con, pol)
            if n:
                log(name, n, "events")
        except Exception as e:  # a Source failure must never cause a herdr reconnect
            log(name, "poll failed:", str(e)[:300])
    process(con, pol)
    try:
        relay(con, pol)
    except Exception as e:  # the relay clock must never cause a herdr reconnect
        log("relay failed:", str(e)[:300])
    try:
        factory.cleanup_closed(con, pol, gone)  # gone in the last pass and still gone now
        factory.auto_cleanup(con, pol)  # at most every 10 minutes
    except Exception as e:  # the cleanup check must never cause a herdr reconnect
        log("cleanup failed:", str(e)[:300])
    page_old_holds(con, pol)


def main():
    log("start", herdr.socket_path(), config.DB)
    while True:
        try:
            con, pol = registry.connect(), config.policy()
            with herdr.subscribe(TYPES) as s:
                resync(con, pol, tokens=True)
                buf, dirty, lost = b"", False, False
                while True:
                    # Drain a burst of events for 0.5 s, then resync once.
                    s.settimeout(0.5 if dirty else TICK)
                    try:
                        chunk = s.recv(65536)
                        if not chunk:
                            raise ConnectionError("herdr closed the socket")
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            if line.strip():
                                msg = json.loads(line)
                                if "error" in msg:
                                    raise RuntimeError(f"subscribe: {msg['error']}")
                                dirty = True
                                lost = lost or "events_lost" in line.decode(errors="replace")
                    except socket.timeout:
                        resync(con, pol, tokens=lost)
                        dirty = lost = False
        except Exception as e:
            log("reconnect after error:", e)
            time.sleep(5)


if __name__ == "__main__":
    if sys.argv[1:] == ["--once"]:
        resync(registry.connect(), config.policy(), tokens=True)
    else:
        main()
