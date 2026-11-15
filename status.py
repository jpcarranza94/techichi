"""The board: one status for each Session from fixed rules, with no AI. See DESIGN.md section "Board".

Pure, like the Router: it reads the registry and the Ref snapshots of the Sources. It never writes, never calls
herdr, and never reads the network.
"""
import json

import registry
import slack_source

GROUPS = ["YOU", "AGENT", "WAITS FOR OTHERS", "BLOCKED", "MERGED, NOT DONE", "CAN CLOSE", "CHECK"]
ORDER = ["fix needed", "ready to merge", "approved", "in review", "draft"]  # the first open PR of a Session wins


def bot(login, pol):
    return login in pol["board"]["bots"] or login.endswith("[bot]")


def pr_status(p, pol):
    """(group, status, detail, since) for one open PR. The first rule that matches wins. A draft has group None."""
    n, since, base = f"#{p['number']}", p["updatedAt"], p["baseRefName"]
    stack = "" if base in pol["board"]["trunks"] else f"  on {base}"
    if p["reviewDecision"] == "CHANGES_REQUESTED":
        return "AGENT", "fix needed", f"{n} changes requested{stack}", since
    if p["failedCheck"]:
        return "AGENT", "fix needed", f"{n} check failed: {p['failedCheck']}{stack}", since
    if p["mergeable"] == "CONFLICTING":
        return "AGENT", "fix needed", f"{n} merge conflict with {base}", since
    if p["isDraft"]:
        return None, "draft", f"{n} draft{stack}", since
    if p["reviewDecision"] == "APPROVED":
        by = ",".join(r["login"] for r in p["latestReviews"] if r["state"] == "APPROVED")
        if p["pendingChecks"]:
            return "WAITS FOR OTHERS", "approved", f"{n} approved by {by}, checks run{stack}", since
        return "YOU", "ready to merge", f"{n} approved by {by}, checks pass{stack}", since
    # A comment-only review keeps the PR in review. GitHub removes a reviewer from the requests when it posts a review.
    who = ",".join(p["reviewRequests"]) or ",".join(
        f"{r['login']} {r['state'].lower()}" for r in p["latestReviews"] if not bot(r["login"], pol)) or "NO REVIEWER"
    return "WAITS FOR OTHERS", "in review", f"{n} {who}{stack}", since


def ticket_of(links, pol):
    """The first ticket Link of a policy project. A hook can record a bogus key, such as UTF-8."""
    return next((r for k, r in links if k == "ticket" and r.split("-")[0] in pol["projects"]), None)


def classify(s, links, need, prs, tickets, pol):
    """(group, status, detail, since) for one Session. links: (kind, ref) pairs. prs and tickets: the snapshots by ref.
    The first rule that matches wins."""
    if need:
        return "YOU", "needs you", need["text"], need["at"]
    if s["agent_status"] == "blocked":
        return "YOU", "needs you", "the agent waits at a prompt in its pane", s["last_seen"]
    urls = [r for k, r in links if k in ("pr", "review")]
    if s["agent_status"] == "working":
        return "AGENT", "working", " ".join(f"#{prs[u]['number']}" for u in urls if u in prs), None
    key = ticket_of(links, pol)
    missing = [u for u in urls if u not in prs]
    if key and key not in tickets and "jira" in pol["factory"]["sources"]:
        missing.append(key)
    if missing:
        return "CHECK", "no data yet", " ".join(missing), None
    mine = [prs[u] for u in urls]
    if s["role"] == "reviewer":
        if not mine:
            return "CHECK", "no PR state", "", None
        p = mine[0]
        n = f"#{p['number']}"
        if p["state"] != "OPEN":
            return "CAN CLOSE", "can close", f"{n} {p['state'].lower()}", p["updatedAt"]
        rev = [r for r in p["latestReviews"] if r["self"]]
        if not rev:
            return "CHECK", "review not posted", n, p["updatedAt"]
        if p["lastCommitAt"] and registry.parse(p["lastCommitAt"]) > registry.parse(rev[0]["submittedAt"]):
            return "AGENT", "check again", f"{n} the author pushed after your review", p["lastCommitAt"]
        return "WAITS FOR OTHERS", "reviewed", f"{n} waits for the author", rev[0]["submittedAt"]
    t = tickets.get(key)
    blockers = [b for b in (t or {}).get("blockers", []) if not b["done"]]
    opened = sorted((pr_status(p, pol) for p in mine if p["state"] == "OPEN"), key=lambda x: ORDER.index(x[1]))
    if opened and opened[0][0]:
        g, st, d, since = opened[0]
        return g, st, "  ".join([d] + [x[2] for x in opened[1:]]), since
    if blockers:
        return "BLOCKED", "blocked", "  ".join(["by " + ", ".join(f"{b['key']} ({b['status']})" for b in blockers)]
                                              + [x[2] for x in opened]), None
    if opened:  # only drafts, the agent is idle, no flag
        return "CHECK", "draft", "  ".join(x[2] for x in opened), opened[0][3]
    nums = " ".join(f"#{p['number']}" for p in mine)
    if mine and t and t["status"] not in pol["cleanup"]["done_statuses"]:
        return "MERGED, NOT DONE", "merged", f"Jira: {t['status']}  {nums}", max(p["updatedAt"] for p in mine)
    if mine:  # a ticket outside Jira: the closed PRs decide
        return "CAN CLOSE", "can close", (f"Jira: {t['status']}, " if t else "") + "PRs closed", None
    return "CHECK", "no PR", f"Jira: {t['status']}" if t else "no ticket", s["last_seen"]


def load(con):
    """The registry rows and the snapshots that the board needs."""
    sessions = con.execute("SELECT * FROM sessions WHERE state != 'ended' ORDER BY created_at").fetchall()
    links = {}
    for r in con.execute("SELECT l.session_id, l.kind, l.ref FROM links l JOIN sessions s USING (session_id) "
                         "WHERE s.state != 'ended' ORDER BY l.created_at, l.kind, l.ref"):
        links.setdefault(r["session_id"], []).append((r["kind"], r["ref"]))
    wait = con.execute("SELECT e.ref, e.reason, p.note FROM events e LEFT JOIN plan p ON p.ticket = e.ref "
                       "WHERE e.kind = 'ticket' AND e.status = 'queued' AND json_extract(e.payload, '$.admitted') "
                       "ORDER BY e.created_at").fetchall()
    refs = sorted({r for ls in links.values() for _, r in ls} | {w["ref"] for w in wait})
    snap = {"pr": {}, "ticket": {}}
    for r in con.execute(f"SELECT kind, ref, state FROM refs WHERE ref IN ({','.join('?' * len(refs))})", refs):
        snap.setdefault(r["kind"], {})[r["ref"]] = json.loads(r["state"])
    return {"sessions": sessions, "links": links, "wait": wait, "prs": snap["pr"], "tickets": snap["ticket"],
            "needs": {r["session_id"]: r for r in con.execute("SELECT * FROM needs")}}


def on_board(s, links, needs):
    """The planner is not a row. A Session with no ticket, PR, or review Link and no flag is outside the factory."""
    return s["role"] != "planner" and (any(k in ("ticket", "pr", "review") for k, _ in links) or s["session_id"] in needs)


def statuses(d, pol):
    """{session_id: (group, status, detail, since)} for each Session on the board."""
    return {s["session_id"]: classify(s, d["links"].get(s["session_id"], []), d["needs"].get(s["session_id"]),
                                      d["prs"], d["tickets"], pol)
            for s in d["sessions"] if on_board(s, d["links"].get(s["session_id"], []), d["needs"])}


def url(pol, kind, ref):
    if kind in ("pr", "review"):
        return ref
    if kind == "ticket" and "jira" in pol:
        return f"{pol['jira']['site']}/browse/{ref}"
    return None


def board(con, pol, names):
    """The whole board as one JSON-ready dict. names: {session_id: herdr agent name}."""
    d = load(con)
    st = statuses(d, pol)
    # since: the time of the status_log row that started the current status, else the time that the rule gives.
    last = {r["session_id"]: r for r in con.execute(
        "SELECT session_id, grp, status, at FROM status_log WHERE rowid IN "
        "(SELECT max(rowid) FROM status_log GROUP BY session_id)")}
    groups = {g: [] for g in GROUPS}
    outside = []
    for s in d["sessions"]:
        sid, ls = s["session_id"], d["links"].get(s["session_id"], [])
        who = names.get(sid) or s["pane_id"] or sid[:8]
        if sid not in st:
            if s["role"] != "planner":
                outside.append(who)
            continue
        g, status, detail, since = st[sid]
        log = last.get(sid)
        if log and (log["grp"], log["status"]) == (g, status):
            since = log["at"]
        key = ticket_of(ls, pol)
        pr = next((d["prs"][r] for k, r in ls if k in ("pr", "review") and r in d["prs"]), None)
        groups[g].append({"session_id": sid, "name": who, "role": s["role"], "project": s["project"], "ticket": key,
                          "title": (d["tickets"].get(key) or {}).get("summary") or (pr or {}).get("title") or "",
                          "status": status, "detail": detail, "since": since, "pane_id": s["pane_id"],
                          "workspace_id": s["workspace_id"],
                          "links": [{"kind": k, "ref": r, "url": url(pol, k, r)} for k, r in ls]})
    for rows in groups.values():
        rows.sort(key=lambda r: r["since"] or "9")  # the oldest first, no time last
    planner = con.execute("SELECT session_id, pane_id, agent_status FROM sessions WHERE role = 'planner' "
                          "AND state != 'ended' ORDER BY created_at DESC").fetchone()
    cursor_of = {"github": "github", "jira": "jira", "slack": "slack:last"}  # the cursor of the last good read
    cur = {r["source"]: r["updated_at"] for r in con.execute("SELECT source, updated_at FROM cursors")}
    return {
        "at": registry.now(),
        "slots": {"used": registry.slots_used(con), "max": pol["spawn"]["max_active"]},
        # paused: the Slack poll runs only in [slack] work_hours and work_days, so an old cursor is no alarm then.
        "sources": [{"name": n, "updated_at": cur.get(cursor_of.get(n, f"source:{n}")),
                     "paused": n == "slack" and "slack" in pol and not slack_source.work_time(pol)}
                    for n in pol["factory"]["sources"]],
        "groups": [{"name": g, "rows": groups[g]} for g in GROUPS],
        "waiting": [{"ticket": w["ref"], "title": (d["tickets"].get(w["ref"]) or {}).get("summary") or "",
                     "reason": w["reason"] or "queued", "note": w["note"] or ""} for w in d["wait"]],
        "outside": outside,
        "unrouted": {r["kind"]: r["n"] for r in con.execute(
            "SELECT kind, count(*) AS n FROM events WHERE status = 'unrouted' GROUP BY kind")},
        "chain": chain(groups, d),
        "planner": dict(planner) if planner else None,
    }


def chain(groups, d):
    """The open "is blocked by" links between the tickets on the board, as a tree. Each root blocks others and is not
    blocked by a ticket on the board. A ticket with two blockers appears under each one."""
    state = {r["ticket"]: f"{r['status']}  {r['detail']}" for rows in groups.values() for r in rows if r["ticket"]}
    for w in d["wait"]:
        state.setdefault(w["ref"], f"waiting: {w['reason'] or 'queued'}")
    title, kids, child = {}, {}, set()
    for key in list(state):  # the tickets on the board and the waiting ones
        t = d["tickets"].get(key)
        title[key] = (t or {}).get("summary", "")
        for b in (t or {}).get("blockers", []):
            if not b["done"]:
                title.setdefault(b["key"], b["summary"])
                state.setdefault(b["key"], f"Jira: {b['status']}, no session")
                kids.setdefault(b["key"], []).append(key)
                child.add(key)

    def node(key, path):
        sub = [node(k, path | {key}) for k in sorted(kids.get(key, [])) if k not in path | {key}]  # a cycle stops
        return {"key": key, "title": title.get(key, ""), "state": state.get(key, ""), "children": sub}

    return [node(k, set()) for k in sorted(kids) if k not in child]
