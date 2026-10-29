"""Check for factory.py claim, status and cleanup. Scratch databases, stubs for herdr, git, Jira and gh.
Run: python3 test_factory.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import herdr  # noqa: E402
import config  # noqa: E402
import os
import tempfile
from pathlib import Path

import factory as f
import registry

pol = config.policy()
con = registry.connect(Path(tempfile.mkdtemp(), "r.db"))
SID, SID2 = "11111111-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000"


def rejects(text):
    try:
        f.parse_ref(pol, text)
    except ValueError:
        return True
    return False


# --- REF forms and rejections ---
assert f.parse_ref(pol, "web-690") == ("ticket", "WEB-690")
assert f.parse_ref(pol, "APP-1325") == ("ticket", "APP-1325")
assert f.parse_ref(pol, "https://github.com/ACME/APP/pull/577/") == ("pr", "https://github.com/acme/app/pull/577")
assert f.parse_ref(pol, "https://acme.slack.com/archives/C0APPCHAN1/p1793197700000002"
                        "?thread_ts=1793197000.000001&cid=C0APPCHAN1") == ("slack", "C0APPCHAN1:1793197000.000001")
assert f.parse_ref(pol, "https://acme.slack.com/archives/C0APPCHAN1/p1793197000000001") == (
    "slack", "C0APPCHAN1:1793197000.000001")
for bad in ("FOO-1", "APP-", "APP-12a", "https://github.com/other/repo/pull/1", "https://github.com/acme/app/issues/5",
            "https://evil.slack.com/archives/C0APPCHAN1/p1793197000000001", "C0APPCHAN1:1793197000.000001", "", "APP-1; rm -rf /"):
    assert rejects(bad), bad

# --- claim: the default session is the caller's pane. A missing Session row is inserted from herdr. ---
AGENTS = [{"name": "web-690-hand", "pane_id": "w4:p5", "workspace_id": "w4", "cwd": f"{Path.home()}/code/web",
           "agent_status": "idle", "agent_session": {"value": SID}},
          {"name": "other", "pane_id": "wZ:p1", "workspace_id": "wZ", "cwd": "/tmp", "agent_status": "working",
           "agent_session": {"value": SID2}}]
calls = []


def fake_herdr(*args):
    calls.append(args)
    if args[:2] == ("agent", "list"):
        return {"agents": AGENTS}
    if args[:2] == ("agent", "get"):
        return {"agent": next(a for a in AGENTS if args[2] in (a["pane_id"], a["name"]))}
    if args[:2] == ("workspace", "list"):
        return {"workspaces": [{"workspace_id": w} for w in WORKSPACES]}
    if args[:2] == ("worktree", "remove"):
        assert "--force" not in args
        WORKSPACES.discard(args[3]) if REMOVE_CLOSES else None
    if args[:2] == ("workspace", "close"):
        WORKSPACES.discard(args[2])
    return {}


herdr.run = fake_herdr
os.environ["HERDR_PANE_ID"] = "w4:p5"
con.execute("INSERT INTO events (id, source, kind, ref, payload, status, reason) VALUES "
            "('j1', 'jira', 'ticket', 'WEB-690', '{\"admitted\": true}', 'queued', 'needs-decision: branch WEB-690-x')")
assert f.claim(con, pol, "web-690")[:3] == ("ticket", "WEB-690", SID)
row = con.execute("SELECT role, state, pane_id, workspace_id, project FROM sessions WHERE session_id = ?", (SID,)).fetchone()
assert tuple(row) == ("worker", "live", "w4:p5", "w4", "WEB")
assert con.execute("SELECT created_by FROM links WHERE kind = 'ticket' AND ref = 'WEB-690'").fetchone()[0] == "claim"
# By name, by pane, by UUID. The row exists already: no second insert.
assert f.claim(con, pol, "https://github.com/acme/app/pull/577", "other")[2] == SID2
assert f.claim(con, pol, "APP-7", "wZ:p1")[2] == SID2 and f.claim(con, pol, "APP-8", SID2)[2] == SID2
assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
del os.environ["HERDR_PANE_ID"]
try:
    f.claim(con, pol, "APP-9")
    raise AssertionError("claim without a session")
except ValueError:
    pass
# With the Link, the admitted ticket routes to the claiming Session and never spawns.
import daemon  # noqa: E402
ev = con.execute("SELECT * FROM events WHERE id = 'j1'").fetchone()
assert daemon.route(con, ev, {**pol, "spawn": {"max_active": 10}})[:2] == ("delivered", SID)

# --- status on the scratch database ---
con.execute("INSERT INTO sessions (session_id, role, state, pane_id) VALUES ('pl', 'planner', 'live', 'wT:p1')")
con.execute("INSERT INTO cursors (source, value, updated_at) VALUES ('github', '2026-10-28T16:40:45Z', '2026-10-28T16:41:00Z')")
con.execute("INSERT INTO events (id, source, kind, ref, payload, status) VALUES ('u1', 'slack', 'inbox', 'operator', '{}', 'unrouted')")
text = f.status(con, {**pol, "spawn": {"max_active": 10}}, names={SID: "web-690-hand"})
for want in ("Sessions: 3 not ended", "web-690-hand", "ticket WEB-690", "PR 577", "Slots: 0 of 10 in use",
             "Waiting admitted tickets: 1", "WEB-690    needs-decision", "Unrouted Events: inbox 1",
             "github           2026-10-28T16:40:45Z", "Planner: pl live", "Relay: none"):
    assert want in text, (want, text)

# --- jira_done: the status name decides, not the category. "Ready for Deploy" is in category done. ---
import jira_source
real_get = jira_source.get
for name, want in (("Ready for Deploy", False), ("In Validation", False), ("Done", True), ("Won't Do", True)):
    jira_source.get = lambda pol_, path, _n=name, **q: {"fields": {"status": {"name": _n, "statusCategory": {"key": "done"}}}}
    assert f.jira_done(pol, "APP-1325") is want, name
jira_source.get = real_get

# --- done: each condition, and each reset of the idle clock ---
W = "aaaaaaaa-0000-0000-0000-000000000001"
con.execute("INSERT INTO sessions (session_id, role, project, cwd, branch, pane_id, workspace_id, agent_status, state) "
            "VALUES (?, 'worker', 'APP', '/wt/APP-1325', 'APP-1325-x', 'wR:p1', 'wR', 'idle', 'live')", (W,))
registry.link(con, "ticket", "APP-1325", W, "spawner")
state = {"jira": True, "pr": True, "clean": True}
f.jira_done = lambda pol_, key: state["jira"]
f.pr_closed = lambda url: state["pr"]
f.clean = lambda cwd: state["clean"]
s = lambda: con.execute("SELECT * FROM sessions WHERE session_id = ?", (W,)).fetchone()
clock = lambda: con.execute("SELECT value FROM cursors WHERE source = ?", (f"cleanup:{W}",)).fetchone()
T0, T30, T61 = "2026-10-28T10:00:00Z", "2026-10-28T10:30:00Z", "2026-10-28T11:01:00Z"
assert f.done(con, pol, s(), T0) == (False, "no PR")  # a Session with no PR is not done
registry.link(con, "pr", "https://github.com/acme/app/pull/574", W, "hook:pr_link")
assert f.done(con, pol, s(), T0) == (False, "idle clock started") and clock()[0] == T0
assert f.done(con, pol, s(), T30) == (False, "idle for less than 60 minutes")
assert f.done(con, pol, s(), T61) == (True, "")
for cond, why in (("jira", "APP-1325 not Done in Jira"), ("pr", "https://github.com/acme/app/pull/574 open"),
                  ("clean", "worktree not clean or not pushed")):
    state[cond] = False
    assert f.done(con, pol, s(), T61) == (False, why) and clock() is None  # reset
    state[cond] = True
    f.done(con, pol, s(), T0)
state["jira"] = None
assert f.done(con, pol, s(), T61) == (False, "APP-1325 not found in Jira") and clock() is None  # a 404 skips
state["jira"] = True
f.done(con, pol, s(), T0)
con.execute("UPDATE sessions SET agent_status = 'working' WHERE session_id = ?", (W,))
assert f.done(con, pol, s(), T61) == (False, "agent working") and clock() is None  # any status change resets
con.execute("UPDATE sessions SET agent_status = 'done' WHERE session_id = ?", (W,))

# --- the Action: order, no --force, no -D, and a stop on a dirty worktree ---
steps = []
WORKSPACES, REMOVE_CLOSES = {"wR"}, False
f.scratch_worktrees = lambda repo, cwd, sid, name: ["/private/tmp/claude-501/x/" + sid + "/validation"]


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def fake_git(*args, cwd=None):
    if args[0] == "rev-parse":  # a linked worktree: its git dir differs from the common dir
        return R(out="/r/.git/worktrees/x\n/r/.git\n")
    steps.append(("git",) + args)
    assert "-D" not in args and "--force" not in args and "push" not in args
    return R()


f.git = fake_git
herdr.run = lambda *a: (steps.append(("herdr",) + a), fake_herdr(*a))[1]
assert f.cleanup_action(con, pol, s())
assert [x[:3] for x in steps if x[1:3] != ("agent", "list")] == [("git", "worktree", "remove"), ("herdr", "workspace", "list"),
                                  ("herdr", "worktree", "remove"), ("herdr", "workspace", "list"), ("herdr", "workspace", "close"),
                                  ("git", "branch", "-d")]
assert steps[-1][3] == "APP-1325-x" and s()["state"] == "ended" and clock() is None
# When worktree remove closes the workspace, no close follows.
con.execute("UPDATE sessions SET state = 'live' WHERE session_id = ?", (W,))
steps.clear()
WORKSPACES, REMOVE_CLOSES = {"wR"}, True
assert f.cleanup_action(con, pol, s()) and ("herdr", "workspace", "close", "wR") not in steps
# A dirty worktree stops before any step. A dirty scratch worktree stops before herdr.
con.execute("UPDATE sessions SET state = 'live' WHERE session_id = ?", (W,))
steps.clear()
state["clean"] = False
assert not f.cleanup_action(con, pol, s()) and [x for x in steps if x[1:3] != ("agent", "list")] == [] and s()["state"] == "live"
f.clean = lambda cwd: "validation" not in cwd
assert not f.cleanup_action(con, pol, s()) and [x for x in steps if x[1:3] != ("agent", "list")] == [] and s()["state"] == "live"
f.clean = lambda cwd: state["clean"]
state["clean"] = True
# The main checkout is never removed: git dir equal to the common dir stops before any step.
f.git = lambda *a, cwd=None: (steps.append(("git",) + a), R(out="/r/.git\n/r/.git\n"))[1]
steps.clear()
assert not f.cleanup_action(con, pol, s()) and [x[:2] for x in steps] == [("git", "rev-parse")] and s()["state"] == "live"
# A shared workspace stops before any step: another agent runs in wR.
f.git = fake_git
AGENTS.append({"name": "second-look", "pane_id": "wR:p2", "workspace_id": "wR", "agent_session": {"value": "someone-else"}})
AGENTS.append({"name": "app-1325", "pane_id": "wR:p1", "workspace_id": "wR", "agent_session": {"value": W}})
steps.clear()
assert not f.cleanup_action(con, pol, s()) and [x[1:3] for x in steps] == [("agent", "list")] and s()["state"] == "live"
AGENTS.pop(-2)  # only the Session itself is left in wR: no stop
WORKSPACES.add("wR")  # the workspace is open, so herdr removes the checkout
steps.clear()
assert f.cleanup_action(con, pol, s()) and ("herdr", "worktree", "remove", "--workspace", "wR") in steps
con.execute("UPDATE sessions SET state = 'live' WHERE session_id = ?", (W,))
# A failed git branch -d stops before the Session ends.
f.git = lambda *a, cwd=None: (steps.append(("git",) + a), R(out="/r/.git/worktrees/x\n/r/.git\n") if a[0] == "rev-parse"
                              else R(1, err="error: the branch is not fully merged") if a[0] == "branch" else R())[1]
assert not f.cleanup_action(con, pol, s()) and s()["state"] == "live"
f.git = fake_git

# --- a workspace closed by hand: gone in two passes in a row, and the workspace absent from herdr ---
logs = []
f.log = lambda *a: logs.append(" ".join(map(str, a)))
con.execute("UPDATE sessions SET state = 'gone' WHERE session_id = ?", (W,))
WORKSPACES, REMOVE_CLOSES = set(), False
steps.clear()
f.cleanup_closed(con, pol, set())  # gone in this pass only, as after a herdr restart: nothing
assert steps == [] and s()["state"] == "gone"
WORKSPACES.add("wR")
f.cleanup_closed(con, pol, {W})  # gone twice, but herdr restored the workspace: nothing
assert [x for x in steps if x[1:3] != ("workspace", "list")] == [] and s()["state"] == "gone"
# A local commit stops the cleanup with one log line. The next pass does not try again.
WORKSPACES.discard("wR")
state["clean"] = False
steps.clear()
f.cleanup_closed(con, pol, {W})
assert s()["state"] == "gone" and [x for x in steps if x[0] == "git"] == []
assert any("stopped: worktree not clean or not pushed" in line for line in logs)
steps.clear()
f.cleanup_closed(con, pol, {W})
assert steps == []
# By hand after the fix: git removes the checkout, because the workspace is closed.
state["clean"] = True
steps.clear()
assert f.cleanup_by_hand(con, pol, "APP-1325") and s()["state"] == "ended"
assert ("git", "worktree", "remove", "/wt/APP-1325") in steps and ("herdr", "worktree", "remove") not in [x[:3] for x in steps]
# A clean checkout: the daemon cleans up by itself.
con.execute("UPDATE sessions SET state = 'gone' WHERE session_id = ?", (W,))
steps.clear()
f.cleanup_closed(con, pol, {W})
assert s()["state"] == "ended" and ("git", "worktree", "remove", "/wt/APP-1325") in steps
assert steps[-1][:4] == ("git", "branch", "-d", "APP-1325-x")
con.execute("UPDATE sessions SET state = 'live' WHERE session_id = ?", (W,))

# --- auto_cleanup: only spawned workers, at most every 10 minutes, and the 24-hour rule ---
for sid, role, by in (("hand", "worker", "claim"), ("plan", "planner", None), ("rel", "relay", None)):
    con.execute("INSERT INTO sessions (session_id, role, project, cwd, agent_status, state) VALUES (?, ?, 'APP', '/x', 'idle', 'live')",
                (sid, role))
    if by:
        registry.link(con, "ticket", "APP-1", sid, by)
con.execute("INSERT INTO sessions (session_id, role, state, last_seen) VALUES ('old', 'worker', 'gone', '2026-10-27T09:00:00Z'), "
            "('new', 'worker', 'gone', '2026-10-28T09:30:00Z')")
checked = []
real_done = f.done
f.done = lambda con_, pol_, s_, now: (checked.append(s_["session_id"]), (False, "test"))[1]
con.execute("DELETE FROM cursors WHERE source = 'cleanup:last'")
f.auto_cleanup(con, pol, now="2026-10-28T10:00:00Z")
assert checked == [W]  # never the hand-started session, the planner, or the relay
gone = dict(con.execute("SELECT session_id, state FROM sessions WHERE session_id IN ('old','new')").fetchall())
assert gone == {"old": "ended", "new": "gone"}  # gone for over 24 hours ends
f.auto_cleanup(con, pol, now="2026-10-28T10:05:00Z")
assert checked == [W]  # within 10 minutes
f.auto_cleanup(con, pol, now="2026-10-28T10:11:00Z")
assert checked == [W, W]
f.done = real_done

# --- cleanup by hand: any worker on the ticket, the clean check stays ---
con.execute("UPDATE sessions SET state = 'live', cwd = '/x', workspace_id = NULL, branch = NULL WHERE session_id = 'hand'")
steps.clear()
assert f.cleanup_by_hand(con, pol, "app-1") and con.execute("SELECT state FROM sessions WHERE session_id = 'hand'").fetchone()[0] == "ended"
assert con.execute("SELECT state FROM sessions WHERE session_id = 'plan'").fetchone()[0] == "live"
try:
    f.cleanup_by_hand(con, pol, "APP-404")
    raise AssertionError("no Session")
except ValueError:
    pass
print("ok")
