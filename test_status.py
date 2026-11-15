"""Check for the board rules, the board dict, and the chain. No herdr, no network. Run: python3 test_status.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import config  # noqa: E402
import json
import tempfile
from pathlib import Path

import registry
import status

pol = config.policy()  # the example: github, jira and slack are Sources
A = "https://github.com/acme/app/pull/"


def pr(n, **kw):
    """A PR snapshot as github_source.snapshot() stores it: open, in review, no reviewer."""
    return {"number": n, "title": f"PR {n}", "state": "OPEN", "isDraft": False, "reviewDecision": "REVIEW_REQUIRED",
            "reviewRequests": [], "latestReviews": [], "failedCheck": None, "pendingChecks": False,
            "mergeable": "MERGEABLE", "baseRefName": "main", "author": "alice", "updatedAt": "2026-11-01T10:00:00Z",
            "lastCommitAt": "2026-11-01T09:00:00Z", **kw}


def review(login, state, at="2026-11-01T09:30:00Z", self=False):
    return {"login": login, "state": state, "submittedAt": at, "self": self}


def ticket(status_="In Progress", blockers=(), summary="Add export"):
    return {"summary": summary, "status": status_, "statusCategory": "indeterminate", "blockers": list(blockers)}


def blocker(key, done=False, st="In Progress"):
    return {"key": key, "summary": f"Blocker {key}", "status": st, "done": done}


def sess(role="worker", agent="idle"):
    return {"session_id": "s", "role": role, "agent_status": agent, "last_seen": "2026-11-01T11:00:00Z"}


def run(links, prs=None, tickets=None, need=None, **kw):
    return status.classify(sess(**kw), links, need, prs or {}, tickets or {}, pol)


T, P = ("ticket", "APP-1"), ("pr", A + "1")
ok = {"APP-1": ticket()}

# A flag wins over all else. A blocked agent also needs the operator.
assert run([T, P], {A + "1": pr(1, failedCheck="test")}, ok, need={"text": "pick a name", "at": "2026-11-01T12:00:00Z"},
           agent="working") == ("YOU", "needs you", "pick a name", "2026-11-01T12:00:00Z")
assert run([T], {}, ok, agent="blocked")[:2] == ("YOU", "needs you")
# A working agent owns the next step, with or without data.
assert run([T, P], {A + "1": pr(1)}, ok, agent="working") == ("AGENT", "working", "#1", None)
assert run([T, P], agent="working")[:2] == ("AGENT", "working")
# A missing snapshot is no crash: no data yet.
assert run([T, P], {}, ok) == ("CHECK", "no data yet", A + "1", None)
assert run([T, P], {A + "1": pr(1)}, {}) == ("CHECK", "no data yet", "APP-1", None)
assert run([("ticket", "UTF-8"), T], {}, ok)[:3] == ("CHECK", "no PR", "Jira: In Progress")  # a bogus key is no ticket

# The PR rules, in order.
g = lambda **kw: run([T, P], {A + "1": pr(1, **kw)}, ok)[:3]
assert g(reviewDecision="CHANGES_REQUESTED", failedCheck="test") == ("AGENT", "fix needed", "#1 changes requested")
assert g(failedCheck="test", mergeable="CONFLICTING") == ("AGENT", "fix needed", "#1 check failed: test")
assert g(mergeable="CONFLICTING") == ("AGENT", "fix needed", "#1 merge conflict with main")
assert g(isDraft=True) == ("CHECK", "draft", "#1 draft")  # only drafts and an idle agent: check it
assert g(reviewDecision="APPROVED", pendingChecks=True, latestReviews=[review("bob", "APPROVED")]) == (
    "WAITS FOR OTHERS", "approved", "#1 approved by bob, checks run")
assert g(reviewDecision="APPROVED", latestReviews=[review("bob", "APPROVED")]) == (
    "YOU", "ready to merge", "#1 approved by bob, checks pass")
assert g(reviewRequests=["bob"]) == ("WAITS FOR OTHERS", "in review", "#1 bob")
# A comment-only review keeps it in review. With no requests, the posters name it. Bots do not count.
assert g(reviewDecision="", latestReviews=[review("coderabbitai", "COMMENTED"), review("dependabot[bot]", "COMMENTED"),
                                           review("bob", "COMMENTED")]) == ("WAITS FOR OTHERS", "in review", "#1 bob commented")
assert g(latestReviews=[review("coderabbitai", "COMMENTED")]) == ("WAITS FOR OTHERS", "in review", "#1 NO REVIEWER")
assert g(baseRefName="APP-0-base") == ("WAITS FOR OTHERS", "in review", "#1 NO REVIEWER  on APP-0-base")  # a stack
assert run([T, P], {A + "1": pr(1)}, ok)[3] == "2026-11-01T10:00:00Z"
# Two open PRs: fix needed wins, and the detail names both.
two = run([T, P, ("pr", A + "2")], {A + "1": pr(1, reviewRequests=["bob"]), A + "2": pr(2, failedCheck="lint")}, ok)
assert two[:3] == ("AGENT", "fix needed", "#2 check failed: lint  #1 bob")

# Blockers: an open blocker blocks a Session with no open PR to act on. A done one does not.
bt = {"APP-1": ticket(blockers=[blocker("APP-7"), blocker("APP-8", done=True, st="Done")])}
assert run([T], {}, bt)[:3] == ("BLOCKED", "blocked", "by APP-7 (In Progress)")
assert run([T, P], {A + "1": pr(1, isDraft=True)}, bt)[:3] == ("BLOCKED", "blocked", "by APP-7 (In Progress)  #1 draft")
assert run([T], {}, {"APP-1": ticket(blockers=[blocker("APP-8", done=True)])})[:2] == ("CHECK", "no PR")

# Merged PRs: the Jira status decides.
merged = {A + "1": pr(1, state="MERGED", updatedAt="2026-11-02T10:00:00Z")}
assert run([T, P], merged, ok) == ("MERGED, NOT DONE", "merged", "Jira: In Progress  #1", "2026-11-02T10:00:00Z")
assert run([T, P], merged, {"APP-1": ticket("Done")})[:3] == ("CAN CLOSE", "can close", "Jira: Done, PRs closed")
assert run([P], merged)[:3] == ("CAN CLOSE", "can close", "PRs closed")  # no ticket: the PRs decide
assert run([T], {}, ok) == ("CHECK", "no PR", "Jira: In Progress", "2026-11-01T11:00:00Z")
nojira = {**pol, "factory": {**pol["factory"], "sources": ["github"]}}
assert status.classify(sess(), [T], None, {}, {}, nojira)[:3] == ("CHECK", "no PR", "no ticket")  # no Jira: no data is fine

# The reviewer branch.
R = ("review", A + "9")
rv = lambda **kw: run([R], {A + "9": pr(9, **kw)}, role="reviewer")[:3]
assert rv(state="CLOSED") == ("CAN CLOSE", "can close", "#9 closed")
assert rv(latestReviews=[review("bob", "APPROVED")]) == ("CHECK", "review not posted", "#9")
mine = [review("op", "COMMENTED", "2026-11-01T09:30:00Z", self=True)]
assert rv(latestReviews=mine) == ("WAITS FOR OTHERS", "reviewed", "#9 waits for the author")
assert rv(latestReviews=mine, lastCommitAt="2026-11-01T09:45:00Z") == (
    "AGENT", "check again", "#9 the author pushed after your review")
assert run([R], {}, role="reviewer")[:2] == ("CHECK", "no data yet")

# The board dict from a registry.
con = registry.connect(Path(tempfile.mkdtemp(), "r.db"))
for sid, role, agent, pane in (("w1", "worker", "idle", "p1"), ("w2", "worker", "idle", "p2"), ("w3", "worker", "idle", "p3"),
                               ("h1", "worker", "idle", "p4"), ("pl", "planner", "idle", "p5"), ("r1", "reviewer", "working", "p6")):
    con.execute("INSERT INTO sessions (session_id, role, project, state, agent_status, pane_id, workspace_id) "
                "VALUES (?, ?, 'APP', 'live', ?, ?, 'ws1')", (sid, role, agent, pane))
con.execute("INSERT INTO sessions (session_id, role, state) VALUES ('old', 'worker', 'ended')")
for kind, ref, sid in (("ticket", "APP-1", "w1"), ("pr", A + "1", "w1"), ("branch", "APP-1", "w1"), ("ticket", "APP-2", "w2"),
                       ("ticket", "APP-3", "w3"), ("review", A + "9", "r1"), ("ticket", "APP-1", "old")):
    registry.link(con, kind, ref, sid, "spawner")
registry.set_ref(con, "pr", A + "1", pr(1, failedCheck="test"))
registry.set_ref(con, "pr", A + "9", pr(9, title="Fix login"))
registry.set_ref(con, "ticket", "APP-1", ticket(summary="Add export"))
# APP-2 and APP-3 are blocked by APP-1. APP-5 waits and is blocked by APP-1 and APP-2: it shows under each.
registry.set_ref(con, "ticket", "APP-2", ticket("To Do", [blocker("APP-1")], "Use export"))
registry.set_ref(con, "ticket", "APP-3", ticket("To Do", [blocker("APP-1"), blocker("APP-4", done=True)], "Docs"))
registry.set_ref(con, "ticket", "APP-5", ticket("To Do", [blocker("APP-1"), blocker("APP-2")], "Report"))
con.execute("INSERT INTO events (id, source, kind, ref, payload, reason) VALUES "
            "('a5', 'jira', 'ticket', 'APP-5', ?, 'needs-plan')", (json.dumps({"admitted": True}),))
con.execute("INSERT INTO plan (ticket, note) VALUES ('APP-5', 'after the export')")
con.execute("INSERT INTO events (id, source, kind, ref, payload, status) VALUES ('u1', 'test', 'slack', 'C1:1.2', '{}', 'unrouted')")
con.execute("INSERT INTO status_log VALUES ('w1', 'AGENT', 'fix needed', '2026-11-01T08:00:00Z')")
con.execute("INSERT INTO status_log VALUES ('w2', 'YOU', 'needs you', '2026-11-01T08:00:00Z')")  # an old status
registry.set_cursor(con, "jira", "2026-11-01T10:00:00Z")
b = status.board(con, pol, {"w1": "app-1"})
json.dumps(b)  # JSON-ready
assert [g["name"] for g in b["groups"]] == status.GROUPS
rows = {r["session_id"]: (g["name"], r) for g in b["groups"] for r in g["rows"]}
assert set(rows) == {"w1", "w2", "w3", "r1"}  # not the planner, not the hand-started h1, not an ended Session
g1, w1 = rows["w1"]
assert g1 == "AGENT" and w1 == {
    "session_id": "w1", "name": "app-1", "role": "worker", "project": "APP", "ticket": "APP-1", "title": "Add export",
    "status": "fix needed", "detail": "#1 check failed: test", "since": "2026-11-01T08:00:00Z", "pane_id": "p1",
    "workspace_id": "ws1", "links": [{"kind": "branch", "ref": "APP-1", "url": None},
                                     {"kind": "pr", "ref": A + "1", "url": A + "1"},
                                     {"kind": "ticket", "ref": "APP-1", "url": "https://acme.atlassian.net/browse/APP-1"}]}
assert rows["w2"][0] == "BLOCKED" and rows["w2"][1]["since"] is None and rows["w2"][1]["name"] == "p2"  # the log is old
assert rows["r1"][0] == "AGENT" and rows["r1"][1]["title"] == "Fix login"
assert b["outside"] == ["p4"] and b["planner"] == {"session_id": "pl", "pane_id": "p5", "agent_status": "idle"}
assert b["waiting"] == [{"ticket": "APP-5", "title": "Report", "reason": "needs-plan", "note": "after the export"}]
assert b["unrouted"] == {"slack": 1} and b["slots"] == {"used": 4, "max": pol["spawn"]["max_active"]}
src = {s["name"]: s["updated_at"] for s in b["sources"]}
assert list(src) == ["github", "jira", "slack"] and src["jira"] and src["github"] is None


def tree(nodes):
    return [(n["key"], tree(n["children"])) for n in nodes]


assert tree(b["chain"]) == [("APP-1", [("APP-2", [("APP-5", [])]), ("APP-3", []), ("APP-5", [])])], tree(b["chain"])
top = b["chain"][0]
assert (top["title"], top["state"]) == ("Add export", "fix needed  #1 check failed: test")
assert top["children"][2]["state"] == "waiting: needs-plan"
# A blocker with no Session, and a cycle that has no root: the walk stops.
registry.set_ref(con, "ticket", "APP-1", ticket(blockers=[blocker("APP-2")]))
assert status.board(con, pol, {})["chain"] == []
registry.set_ref(con, "ticket", "APP-1", ticket(blockers=[blocker("WEB-1", st="To Do"), blocker("APP-2")]))
c = status.board(con, pol, {})["chain"]
assert (c[0]["key"], c[0]["state"]) == ("WEB-1", "Jira: To Do, no session")
assert tree(c) == [("WEB-1", [("APP-1", [("APP-2", [("APP-5", [])]), ("APP-3", []), ("APP-5", [])])])], tree(c)
print("ok")
