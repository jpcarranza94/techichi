#!/usr/bin/env python3
"""The operator's commands for the factory. See DESIGN.md section "Commands".

  factory.py claim REF [--session PANE|NAME|UUID]   link a Session to a ticket, a PR, or a Slack thread
  factory.py status                                 the whole factory, read only
  factory.py cleanup KEY                            end the worker Session of a ticket and remove its checkout
"""
import argparse
import os
import re
import subprocess
import sys
import time
from datetime import timedelta

import config
import harness
import herdr
import registry

NOW = registry.NOW
IDLE_FOR = timedelta(minutes=60)
EVERY = timedelta(minutes=10)


def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), "factory:", *a, file=sys.stderr, flush=True)


def git(*args, cwd=None):
    return subprocess.run([config.GIT, *(["-C", str(cwd)] if cwd else []), *args],
                          capture_output=True, text=True, timeout=60)


# --- claim ---------------------------------------------------------------------------------------------------

def parse_ref(pol, text):
    """Return (kind, ref) in the form that the Sources and hooks store, or raise ValueError."""
    t = text.strip()
    keys = "|".join(map(re.escape, pol["projects"]))
    if re.fullmatch(rf"(?i)(?:{keys})-\d+", t):
        return "ticket", t.upper()
    m = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?", t)
    if m:
        repos = {p["github"].lower(): p["github"] for p in pol["projects"].values() if "github" in p}
        if m[1].lower() in repos:
            return "pr", f"https://github.com/{repos[m[1].lower()]}/pull/{m[2]}"  # the pr_link.py form
        raise ValueError(f"{m[1]} is not a policy repo")
    m = re.fullmatch(r"https://([a-z0-9-]+\.slack\.com)/archives/([A-Z0-9]+)/p(\d{10})(\d{6})(\?[\w.=&%-]*)?", t)
    if m:
        if m[1] not in pol.get("slack", {}).get("hosts", []):
            raise ValueError(f"{m[1]} is not in [slack] hosts")
        thread = re.search(r"[?&]thread_ts=(\d{10}\.\d{6})", m[5] or "")
        return "slack", f"{m[2]}:{thread[1] if thread else m[3] + '.' + m[4]}"  # the thread root, as in Delivery
    raise ValueError("REF must be a ticket key of a policy project, a PR URL of a policy repo, or a Slack permalink")


def claim(con, pol, text, target=None):
    kind, ref = parse_ref(pol, text)
    target = target or os.environ.get("HERDR_PANE_ID")
    if not target:
        raise ValueError("no --session and no HERDR_PANE_ID: run it inside a herdr pane or name the session")
    a = herdr.find_agent(target)
    sid = herdr.session_id(a)
    if not sid:
        raise ValueError(f"herdr shows no Claude session in {a.get('pane_id')}")
    if not con.execute("SELECT 1 FROM sessions WHERE session_id = ?", (sid,)).fetchone():
        con.execute("INSERT INTO sessions (session_id, role, project, cwd, pane_id, workspace_id, agent_status, state, "
                    f"last_seen) VALUES (?, 'worker', ?, ?, ?, ?, ?, 'live', {NOW})",
                    (sid, registry.project_for_cwd(a.get("cwd") or ".", pol), a.get("cwd"), a.get("pane_id"),
                     a.get("workspace_id"), a.get("agent_status")))
    registry.link(con, kind, ref, sid, "claim")
    return kind, ref, sid, a.get("name") or a.get("pane_id")


# --- status --------------------------------------------------------------------------------------------------

def status(con, pol, names=None):
    out = []
    if names is None:
        try:
            names = {herdr.session_id(a): a.get("name") for a in herdr.agents()}
        except Exception:
            names = {}
    rows = con.execute("SELECT * FROM sessions WHERE state != 'ended' ORDER BY role, created_at").fetchall()
    out.append(f"Sessions: {len(rows)} not ended")
    for s in rows:
        links = con.execute("SELECT kind, ref FROM links WHERE session_id = ? ORDER BY kind, ref", (s["session_id"],)).fetchall()
        tickets = ",".join(r["ref"] for r in links if r["kind"] == "ticket") or "-"
        prs = ",".join(r["ref"].rsplit("/", 1)[-1] for r in links if r["kind"] == "pr") or "-"
        held = con.execute("SELECT count(*) FROM events WHERE status = 'held' AND session_id = ?", (s["session_id"],)).fetchone()[0]
        who = names.get(s["session_id"]) or s["pane_id"] or "-"
        out.append(f"  {s['session_id'][:8]} {s['role']:<11} {who:<20} {s['state']:<10} {s['agent_status'] or '-':<8} "
                   f"ticket {tickets:<10} PR {prs:<10} held {held}")
    out.append(f"Slots: {registry.slots_used(con)} of {pol['spawn']['max_active']} in use")
    wait = con.execute("SELECT ref, reason FROM events WHERE kind = 'ticket' AND status = 'queued' "
                       "AND json_extract(payload, '$.admitted') ORDER BY created_at").fetchall()
    out.append(f"Waiting admitted tickets: {len(wait)}")
    out += [f"  {w['ref']:<10} {w['reason'] or 'queued'}" for w in wait]
    unrouted = con.execute("SELECT kind, count(*) AS n FROM events WHERE status = 'unrouted' GROUP BY kind").fetchall()
    out.append("Unrouted Events: " + (", ".join(f"{u['kind']} {u['n']}" for u in unrouted) or "none"))
    out.append("Sources:")
    for c in con.execute("SELECT source, value, updated_at FROM cursors WHERE source IN "
                         "('github','github:next','jira','jira:next','slack','slack:poll_next') "
                         "OR source LIKE 'source:%' ORDER BY source"):
        out.append(f"  {c['source']:<16} {c['value']:<28} written {c['updated_at']}")
    for role in ("planner", "relay"):
        r = con.execute("SELECT session_id, state, agent_status, pane_id FROM sessions WHERE role = ? AND state != 'ended' "
                        "ORDER BY created_at DESC", (role,)).fetchone()
        out.append(f"{role.capitalize()}: " + (f"{r['session_id'][:8]} {r['state']} {r['agent_status'] or '-'} in {r['pane_id']}"
                                              if r else "none"))
    return "\n".join(out)


# --- cleanup -------------------------------------------------------------------------------------------------

def jira_done(pol, key):
    """True when the ticket's status name is in [cleanup] done_statuses. None when Jira has no such ticket (404).
    The status category is not enough: Jira puts "Ready for Deploy" in category done, but the deploy and the
    operator's validation come after it. A ticket in that status almost lost its session once."""
    import jira_source
    try:
        f = jira_source.get(pol, f"/issue/{key}", fields="status")["fields"]
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    return f["status"]["name"] in pol["cleanup"]["done_statuses"]


def pr_closed(url):
    r = subprocess.run([config.GH, "pr", "view", url, "--json", "state", "-q", ".state"],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0 and r.stdout.strip() in ("MERGED", "CLOSED")


def clean(cwd):
    """No change in the checkout, and no commit that no remote branch has."""
    st = git("status", "--porcelain", cwd=cwd)
    ahead = git("rev-list", "--count", "HEAD", "--not", "--remotes", cwd=cwd)
    return st.returncode == 0 and not st.stdout.strip() and ahead.returncode == 0 and ahead.stdout.strip() == "0"


def done(con, pol, s, now):
    """Return (True, '') when a spawned worker's work is really done. Any failed condition resets the idle clock."""
    key = f"cleanup:{s['session_id']}"

    def no(why):
        registry.drop_cursor(con, key)
        return False, why

    if s["agent_status"] not in ("idle", "done"):
        return no(f"agent {s['agent_status']}")
    links = con.execute("SELECT kind, ref FROM links WHERE session_id = ?", (s["session_id"],)).fetchall()
    tickets = [r["ref"] for r in links if r["kind"] == "ticket"]
    prs = [r["ref"] for r in links if r["kind"] == "pr"]
    for t in tickets:
        if not jira_ticket(con, pol, t):
            continue  # another Source admitted it: the PRs, the idle time and the clean checkout decide
        d = jira_done(pol, t)
        if d is None:
            return no(f"{t} not found in Jira")
        if not d:
            return no(f"{t} not Done in Jira")
    if "github" not in pol["projects"].get(s["project"], {}):
        # Another forge: no PR check. A Done Jira ticket is then the only signal that the work ended.
        if not any(jira_ticket(con, pol, t) for t in tickets):
            return no("no GitHub and no Jira: run factory.py cleanup KEY")
        prs = []
    elif not prs:
        return no("no PR")
    for p in prs:
        if not pr_closed(p):
            return no(f"{p} open")
    if not s["cwd"] or not clean(s["cwd"]):
        return no("worktree not clean or not pushed")
    if not registry.cursor(con, key):
        registry.set_cursor(con, key, now)  # the first eligible time
        return False, "idle clock started"
    if not registry.due(con, key, IDLE_FOR, at=now):
        return False, "idle for less than 60 minutes"
    return True, ""


def jira_ticket(con, pol, key):
    """True when the Jira Source admitted the ticket. A ticket with no admission Event counts when Jira is on."""
    r = con.execute("SELECT source FROM events WHERE kind = 'ticket' AND ref = ? AND json_extract(payload, '$.admitted') "
                    "ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
    return r["source"] == "jira" if r else "jira" in pol["factory"]["sources"]


def scratch_worktrees(repo, cwd, sid, name):
    """Git worktrees that the session made in the scratch folder of its harness `name`."""
    prefix = harness.scratch_dir(name, cwd, sid)
    if not prefix:
        return []
    lines = git("worktree", "list", "--porcelain", cwd=repo).stdout.splitlines()
    return [l.split(" ", 1)[1] for l in lines if l.startswith("worktree ") and os.path.realpath(l.split(" ", 1)[1]).startswith(prefix)]


def cleanup_action(con, pol, s):
    """Remove the checkouts, the workspace and the branch, then end the Session. Stop at the first failure."""
    ticket = (con.execute("SELECT ref FROM links WHERE kind = 'ticket' AND session_id = ?", (s["session_id"],)).fetchone()
              or ["-"])[0]
    project = s["project"] or registry.project_for_cwd(s["cwd"] or ".", pol)
    repo = pol["projects"][project]["repo"] if project in pol["projects"] else None

    def stop(why):
        log(f"cleanup of {ticket} ({s['session_id'][:8]}) stopped: {why}")
        return False

    if not repo or not s["cwd"]:
        return stop("no repo or no cwd")
    # A hand-started session can work in the main checkout itself. Only a linked worktree may go.
    dirs = git("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", cwd=s["cwd"])
    d = dirs.stdout.split()
    if dirs.returncode or len(d) != 2 or d[0] == d[1]:
        return stop(f"{s['cwd']} is not a linked worktree")
    try:
        agents = [a for a in herdr.agents() if herdr.session_id(a) != s["session_id"]]
    except Exception as e:
        return stop(f"herdr agent list: {e}")
    # herdr worktree remove closes the whole workspace and kills every pane in it.
    others = [a.get("name") or a.get("pane_id") for a in agents if s["workspace_id"] and a.get("workspace_id") == s["workspace_id"]]
    if others:
        return stop(f"workspace {s['workspace_id']} also runs {', '.join(map(str, others))}")
    # Another Session can work in the same checkout, for example a second spawn of the same ticket.
    users = [a.get("name") or a.get("pane_id") for a in agents if inside(a.get("cwd"), s["cwd"])]
    users += [r["session_id"][:8] for r in con.execute("SELECT session_id, cwd FROM sessions WHERE session_id != ? "
                                                       "AND state IN ('live','registered')", (s["session_id"],))
              if inside(r["cwd"], s["cwd"])]
    if users:
        return stop(f"{s['cwd']} is also used by {', '.join(map(str, users))}")
    if not clean(s["cwd"]):
        return stop("worktree not clean or not pushed")
    for wt in scratch_worktrees(repo, s["cwd"], s["session_id"], harness.of_project(pol, project)):  # 1.
        if not clean(wt):
            return stop(f"scratch worktree {wt} not clean")
        r = git("worktree", "remove", wt, cwd=repo)
        if r.returncode:
            return stop(f"git worktree remove {wt}: {r.stderr.strip()[:200]}")
    try:  # 2. never --force
        if s["workspace_id"] and s["workspace_id"] in open_workspaces():
            herdr.worktree_remove(s["workspace_id"])
            if s["workspace_id"] in open_workspaces():
                herdr.workspace_close(s["workspace_id"])
        else:  # the operator closed the workspace by hand. The checkout stays, so git removes it.
            r = git("worktree", "remove", s["cwd"], cwd=repo)
            if r.returncode:
                return stop(f"git worktree remove {s['cwd']}: {r.stderr.strip()[:200]}")
    except Exception as e:
        return stop(f"herdr: {e}")
    if s["branch"]:  # 3. never -D, never a remote branch
        r = git("branch", "-d", s["branch"], cwd=repo)
        if r.returncode and "not found" not in r.stderr:
            return stop(f"git branch -d {s['branch']}: {r.stderr.strip()[:200]}")
    con.execute(f"UPDATE sessions SET state = 'ended', ended_at = {NOW} WHERE session_id = ?", (s["session_id"],))  # 4.
    registry.drop_cursor(con, f"cleanup:{s['session_id']}")
    registry.drop_cursor(con, f"closed:{s['session_id']}")
    log(f"cleanup of {ticket}: Session {s['session_id'][:8]} ended, checkout, workspace and branch removed")
    return True


def inside(path, root):
    """True when path is root or a folder in it."""
    if not path:
        return False
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def checkout_left(s):
    """True when the checkout of a Session still exists."""
    return bool(s["cwd"]) and os.path.isdir(s["cwd"])


def open_workspaces():
    return {w["workspace_id"] for w in herdr.workspaces()}


def cleanup_closed(con, pol, gone_before):
    """A spawned worker whose workspace the operator closed by hand. Claude Code runs its SessionEnd hook when the
    workspace closes, so the Session is ended. An agent with no hook is gone. Either way the checkout stays.
    A herdr restart can stop every agent for a while, so the Session must be gone or ended in two passes in a row
    (gone_before), its checkout must still exist, and its workspace must be absent from herdr. herdr restores
    workspaces with their ids, so a restored workspace counts as open. One try for each Session: a stop writes
    one log line, and factory.py cleanup KEY can try again."""
    rows = [s for s in con.execute("SELECT DISTINCT s.* FROM sessions s JOIN links l USING (session_id) WHERE s.role IN "
                                   "('worker','coordinator') AND s.state IN ('gone','ended') AND l.kind = 'ticket' "
                                   "AND l.created_by = 'spawner'").fetchall()
            if s["session_id"] in gone_before and s["workspace_id"] and checkout_left(s)
            and not registry.cursor(con, f"closed:{s['session_id']}")]
    if not rows:
        return
    ws = open_workspaces()
    for s in rows:
        if s["workspace_id"] not in ws:
            registry.set_cursor(con, f"closed:{s['session_id']}", registry.now())
            log(f"workspace {s['workspace_id']} of {s['session_id'][:8]} is closed, cleanup starts")
            cleanup_action(con, pol, s)


def auto_cleanup(con, pol, now=None):
    """The daemon's check, at most every 10 minutes. Only spawned workers. Also ends Sessions gone for 24 hours."""
    now = now or registry.now()
    if not registry.due(con, "cleanup:last", EVERY, at=now):
        return
    registry.set_cursor(con, "cleanup:last", now)
    con.execute(f"UPDATE sessions SET state = 'ended', ended_at = {NOW} WHERE state = 'gone' "
                "AND coalesce(last_seen, created_at) < strftime('%Y-%m-%dT%H:%M:%SZ', ?, '-24 hours')", (now,))
    for s in con.execute("SELECT DISTINCT s.* FROM sessions s JOIN links l USING (session_id) WHERE s.role IN "
                         "('worker','coordinator') AND s.state = 'live' AND l.kind = 'ticket' AND l.created_by = 'spawner'"
                         ).fetchall():
        try:
            ok, why = done(con, pol, s, now)
        except Exception as e:
            log(f"cleanup check of {s['session_id'][:8]} failed: {str(e)[:200]}")
            continue
        if ok:
            cleanup_action(con, pol, s)


def cleanup_by_hand(con, pol, key):
    rows = [s for s in con.execute("SELECT DISTINCT s.* FROM sessions s JOIN links l USING (session_id) WHERE l.kind = 'ticket' "
                                   "AND l.ref = ? AND s.role IN ('worker','coordinator')", (key.upper(),)).fetchall()
            if s["state"] != "ended" or checkout_left(s)]  # an ended Session counts while its checkout is left
    if not rows:
        raise ValueError(f"no worker Session has ticket {key.upper()}")
    return all([cleanup_action(con, pol, s) for s in rows])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim")
    c.add_argument("ref")
    c.add_argument("--session", help="pane id, agent name, or Claude session UUID. Default: this pane.")
    sub.add_parser("status")
    k = sub.add_parser("cleanup")
    k.add_argument("key")
    a = ap.parse_args(argv)
    con, pol = registry.connect(), config.policy()
    try:
        if a.cmd == "claim":
            kind, ref, sid, who = claim(con, pol, a.ref, a.session)
            print(f"Linked {kind} {ref} to Session {sid} ({who}).")
        elif a.cmd == "status":
            print(status(con, pol))
        else:
            sys.exit(0 if cleanup_by_hand(con, pol, a.key) else 1)
    except ValueError as e:
        sys.exit(f"{a.cmd}: {e}")


if __name__ == "__main__":
    main()
