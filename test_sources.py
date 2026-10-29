"""Check for the command Source, admission without Jira, the derived PR Link, and the GitHub head branch.
Run: python3 test_sources.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import command_source
import config
import daemon
import factory
import github_source
import registry

d = Path(tempfile.mkdtemp())
con = registry.connect(d / "r.db")
script = d / "events.py"


def events(*lines, code=0, sleep=0):
    """A command Source that prints these lines. Each line can use $SINCE for TECHICHI_SINCE."""
    script.write_text("import os, sys, time\n"
                      f"time.sleep({sleep})\n"
                      f"for l in {list(lines)!r}:\n"
                      "    print(l.replace('$SINCE', os.environ['TECHICHI_SINCE']))\n"
                      f"sys.exit({code})\n")


raw = {**config.policy(), "factory": {"sources": ["github", "tickets"]},
       "sources": {"tickets": {"command": [sys.executable, str(script)], "timeout_seconds": 2}}}
pol = config.complete({k: v for k, v in raw.items() if k not in ("jira", "slack")})
assert pol["factory"]["sources"] == ["github", "tickets"] and pol["sources"]["tickets"]["every_seconds"] == 60
try:
    config.complete({**raw, "factory": {"sources": ["tickets", "gitlab"]}})
    raise AssertionError("a source with no command passed")
except config.PolicyError as e:
    assert "source 'gitlab' needs [sources.gitlab] command" in str(e)


def ev(id_, kind="ticket", ref="APP-1", **payload):
    return json.dumps({"id": id_, "kind": kind, "ref": ref, "payload": {"what": "ready", "actor": "bot", **payload}})


# --- The command Source: good lines in, bad lines out and never echoed, TECHICHI_SINCE, the cursor ---
events(ev("a1", what="since=$SINCE"),
       ev("a2", ref="ISS-7", admitted=True, project="WEB", slug="fix-the-login"),
       ev("a3", ref="APP-8", admitted=True, slug="Bad Slug!"),
       "not json SECRET-TEXT", json.dumps(["a list"]),
       ev("b1", kind="email"), ev("b2", ref="iss 7", admitted=True), ev("b3", ref="ZZZ-1", admitted=True),
       ev("b4", kind="pr", ref="https://github.com/acme/app/pull/1", admitted=True), ev("b5", ref="x\ny"))
err = io.StringIO()
with contextlib.redirect_stderr(err):
    assert command_source.poll(con, pol, "tickets") == 3
assert err.getvalue().count("skipped") == 7 and "SECRET-TEXT" not in err.getvalue()
rows = {r["id"]: (r["source"], r["kind"], r["ref"], json.loads(r["payload"])) for r in con.execute("SELECT * FROM events")}
assert set(rows) == {"tickets:a1", "tickets:a2", "tickets:a3"}
assert rows["tickets:a1"][3]["what"] == "since=" and rows["tickets:a1"][0] == "tickets"  # first run: empty SINCE
assert rows["tickets:a2"][3] == {"what": "ready", "actor": "bot", "self": False, "admitted": True, "project": "WEB",
                                 "slug": "fix-the-login"}
assert rows["tickets:a3"][3]["project"] == "APP" and "slug" not in rows["tickets:a3"][3]  # prefix project, bad slug dropped
since = registry.cursor(con, "source:tickets")
assert since and not registry.due(con, "source:tickets:next")
assert command_source.poll(con, pol, "tickets") == 0  # inside every_seconds: no run
# The next run gets the start time of the last good run. A failed run moves no cursor.
registry.drop_cursor(con, "source:tickets:next")
events(ev("a4", what="since=$SINCE"))
assert command_source.poll(con, pol, "tickets") == 1
assert json.loads(con.execute("SELECT payload FROM events WHERE id = 'tickets:a4'").fetchone()[0])["what"] == f"since={since}"
good = registry.cursor(con, "source:tickets")
for bad_run, why in ((dict(code=3), "exit 3"), (dict(sleep=5), "timeout after 2 s")):
    registry.drop_cursor(con, "source:tickets:next")
    events(ev("a5"), **bad_run)
    try:
        command_source.poll(con, pol, "tickets")
        raise AssertionError("a failed run passed")
    except RuntimeError as e:
        assert why in str(e)
    assert registry.cursor(con, "source:tickets") == good and not registry.due(con, "source:tickets:next")
# The dry run prints and writes nothing.
n = con.execute("SELECT count(*) FROM events").fetchone()[0]
events(ev("a6"))
with contextlib.redirect_stdout(io.StringIO()) as o:
    assert command_source.poll(con, pol, "tickets", dry_run=True) == 1
assert '"tickets:a6"' in o.getvalue() and con.execute("SELECT count(*) FROM events").fetchone()[0] == n
# The daemon runs it from the policy list. Status shows its cursor.
assert "  source:tickets " in factory.status(con, pol, names={})

# --- Admission without Jira: the command Source's ticket spawns. Cleanup does not ask Jira for it. ---
spawned = []
pol["spawn"] = {"max_active": 5, "gap_seconds": 0}
con.execute("UPDATE events SET status = 'dropped' WHERE ref != 'ISS-7'")
daemon.process(con, pol, prompt=lambda *a: None, spawn=lambda key, slug: spawned.append((key, slug)) or "s-iss7",
               start_planner=lambda t: None)
assert spawned == [("ISS-7", "fix-the-login")]
assert not factory.jira_ticket(con, pol, "ISS-7")  # a command Source admitted it
assert factory.jira_ticket(con, {**pol, "factory": {**pol["factory"], "sources": ["jira"]}}, "NEVER-1")  # no admission, Jira on
asked = []
factory.jira_done = lambda pol_, key: asked.append(key) or True
factory.pr_closed, factory.clean = lambda url: True, lambda cwd: True
con.execute("INSERT INTO sessions (session_id, role, project, cwd, agent_status, state) VALUES ('s-iss7', 'worker', 'WEB', '/w', 'idle', 'live')")
registry.link(con, "ticket", "ISS-7", "s-iss7", "spawner")
registry.link(con, "pr", "https://github.com/acme/web/pull/3", "s-iss7", "hook:pr_link")
s = con.execute("SELECT * FROM sessions WHERE session_id = 's-iss7'").fetchone()
assert factory.done(con, pol, s, "2026-10-28T10:00:00Z") == (False, "idle clock started") and asked == []

# --- The derived PR Link: only for a harness with no recorder, or a project with no github (another forge) ---
nogh = {k: v for k, v in pol["projects"]["APP"].items() if k != "github"}
for name, links, projects in (("claude", 0, pol["projects"]), ("pi", 1, pol["projects"]),
                              ("claude", 1, {**pol["projects"], "APP": nogh})):
    hp = {**pol, "factory": {**pol["factory"], "harness": name}, "projects": projects}
    c = registry.connect(Path(tempfile.mkdtemp(), "h.db"))
    c.execute("INSERT INTO sessions (session_id, role, project, agent_status, state, pane_id) "
              "VALUES ('w1', 'worker', 'APP', 'idle', 'live', 'p1')")
    registry.link(c, "branch", "APP-7-x", "w1", "spawner")
    c.execute("INSERT INTO events (id, source, kind, ref, payload) VALUES ('gh:1', 'github', 'pr', ?, ?)",
              ("https://github.com/acme/app/pull/9", json.dumps({"what": "review", "head": "APP-7-x"})))
    got = []
    daemon.process(c, hp, prompt=lambda pane, text: got.append(pane), spawn=lambda *a: None, start_planner=lambda t: None)
    made = c.execute("SELECT created_by FROM links WHERE kind = 'pr'").fetchall()
    assert len(made) == links and got == (["p1"] if links else []), name
    assert not made or made[0][0] == "derived:branch"

# A command Source keeps a safe head branch on a pr Event only. A GitLab MR links the same way.
mr = {"id": "m1", "kind": "pr", "ref": "https://gitlab.example.com/acme/app/-/merge_requests/9", "payload": {"head": "APP-7-x"}}
assert command_source.check(json.dumps(mr), pol, "mrs")["payload"]["head"] == "APP-7-x"
for bad in ("../x", "a b", "x;rm", ""):
    assert "head" not in command_source.check(json.dumps({**mr, "payload": {"head": bad}}), pol, "mrs")["payload"], bad
assert "head" not in command_source.check(json.dumps({**mr, "kind": "ticket", "ref": "APP-7"}), pol, "mrs")["payload"]

# --- The GitHub Source reads the head branch only when a harness with no recorder is in use ---
note = {"id": "n", "updated_at": "2026-10-28T15:44:49Z", "repository": {"full_name": "acme/app"},
        "subject": {"type": "PullRequest", "url": "https://api.github.com/repos/acme/app/pulls/9"}}
act = [{"id": 1, "user": {"login": "bob"}, "created_at": "2026-10-28T15:40:00Z", "html_url": "u"}]
fetch = lambda p: act if "issues/9/comments" in p else []  # noqa: E731
for name, head in (("claude", None), ("pi", "APP-7-x")):
    hp = {**pol, "factory": {**pol["factory"], "harness": name}}
    c = registry.connect(Path(tempfile.mkdtemp(), "g.db"))
    reads = []
    with contextlib.redirect_stdout(io.StringIO()) as o:
        github_source.poll(c, hp, dry_run=True, since="2026-10-28T15:00:00Z", fetch=fetch, login=lambda: "me",
                           list_fn=lambda p: ({}, [note]), get=lambda p: reads.append(p) or {"head": {"ref": "APP-7-x"}})
    payload = json.loads(o.getvalue().splitlines()[0])["payload"]
    assert payload.get("head") == head and reads == ([] if head is None else ["repos/acme/app/pulls/9"]), name
print("ok")
