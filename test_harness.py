"""Conformance test for the harness adapters, the fake included, and a spawn through the fake harness.
Run: python3 test_harness.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import json
import os
import tempfile
from pathlib import Path

import config
import daemon
import harness
import herdr
import registry
import spawn

U = "01a17acd-3f71-71db-ae09-22feaf3b79d2"
# What herdr reports as agent_session for each harness. A harness with no entry here fails the test:
# its session id mapping must be shown, as herdr reports it (an id, or a session file path).
REPORTED = {
    "claude": {"kind": "id", "source": "herdr:claude", "value": U},
    "pi": {"kind": "path", "source": "herdr:pi", "value": f"/h/.pi/agent/sessions/--x--/2026-10-27T00-40-33-905Z_{U}.jsonl"},
    "fake": {"kind": "id", "value": U},
}
pol = config.policy()

# --- The contract, for every entry ---
for name, h in harness.HARNESSES.items():
    assert h["kind"] in harness.HERDR_KINDS, (name, "herdr agent start --kind does not accept it")
    assert h["session"].startswith("-"), (name, "the session flag")
    assert h["skills"] in ("link", "arg"), name
    if h["skills"] == "link":
        assert isinstance(h["skills_dir"], Path), name
    else:
        assert h["skill_flag"].startswith("-"), name
    assert isinstance(h["recorder"], bool) and isinstance(h["args"], list), name
    assert h["scratch"] is None or "{sid}" in h["scratch"], name
    args = harness.start_args(name, "SID-1", pol, [Path("/skills/w")])
    assert args[:2] == [h["session"], "SID-1"], (name, "the session id comes first, picked before the start")
    assert ("/skills/w" in args) == (h["skills"] == "arg"), (name, "an arg harness gets the skill folder")
    assert name in REPORTED, (name, "add what herdr reports as its agent_session to REPORTED")
    assert harness.session_id({"agent": h["kind"], "agent_session": REPORTED[name]}) == U, (name, "session id mapping")
assert harness.session_id({}) is None

# Claude keeps the exact start line of the factory before the adapters existed.
assert harness.start_args("claude", "S", pol, [Path("/x")]) == ["--session-id", "S", "--permission-mode", "auto"]
# Extra args from the policy come after the harness args.
assert harness.start_args("pi", "S", {**pol, "harness": {"pi": {"args": ["--model", "m"]}}}) == ["--session-id", "S", "--model", "m"]
# The scratch folder is under the real /tmp: /private/tmp on macOS, /tmp on Linux.
assert harness.scratch_dir("claude", "/w", "S") == os.path.join(os.path.realpath("/tmp"), f"claude-{os.getuid()}/-w/S/")
assert harness.scratch_dir("pi", "/w", "S") is None

# --- Selection: the default, a project override, and the herdr kinds that sync matches ---
two = {**pol, "projects": {**pol["projects"], "WEB": {**pol["projects"]["WEB"], "harness": "pi"}}}
assert harness.of_project(two, "APP") == "claude" and harness.of_project(two, "WEB") == "pi"
assert harness.in_use(two) == {"claude", "pi"} and harness.kinds(two) == {"claude", "pi"}
try:
    config.complete({**pol, "factory": {"harness": "nope"}})
    raise AssertionError("an unknown harness passed")
except config.PolicyError as e:
    assert "no harness 'nope'" in str(e)

# --- sync matches a Pi agent that reports a session file path to the id that the spawner passed ---
con = registry.connect(Path(tempfile.mkdtemp(), "r.db"))
con.execute("INSERT INTO sessions (session_id, role, project, state) VALUES (?, 'worker', 'WEB', 'registered')", (U,))
daemon.sync(con, {"agents": [{"agent": "pi", "agent_session": REPORTED["pi"], "pane_id": "w1:p1", "agent_status": "idle"},
                             {"agent": "codex", "agent_session": {"kind": "id", "value": "other"}, "pane_id": "w2:p1"}]}, two)
assert [tuple(r) for r in con.execute("SELECT session_id, state, pane_id FROM sessions")] == [(U, "live", "w1:p1")]  # codex: not in use

# --- A spawn through the fake harness: the herdr kind, the session flag, and the skill folder on the start line ---
fake = {**pol, "projects": {**pol["projects"], "WEB": {**pol["projects"]["WEB"], "harness": "fake", "setup": []}}}
WT = tempfile.mkdtemp()
runs, calls = [], []


def fake_run(*args):
    runs.append(args)
    if args[:2] == ("worktree", "create"):
        return {"root_pane": {"pane_id": "w9:p1"}, "workspace": {"workspace_id": "w9"}, "worktree": {"path": WT}}
    if args[:2] == ("pane", "process-info"):
        return {"process_info": {"shell_pid": 42}}
    if args[:2] == ("agent", "get"):
        return {"agent": {"interactive_ready": True}}
    return {}


herdr.run, herdr.call = fake_run, lambda method, params=None: calls.append((method, params)) or {}
spawn.run = lambda cmd, **kw: "main\n"  # gh repo view and git fetch
spawn.existing_work = lambda key, p: []
db = Path(tempfile.mkdtemp(), "s.db")
c2 = registry.connect(db)
# The admission came from a command Source: the ref has no project prefix, payload.project names it.
c2.execute("INSERT INTO events (id, source, kind, ref, payload) VALUES ('t:1', 'tickets', 'ticket', 'ISS-5', ?)",
           (json.dumps({"admitted": True, "project": "WEB"}),))
out = spawn.spawn("ISS-5", "fix-it", pol=fake, db=db)
start = next(a for a in runs if a[:2] == ("agent", "start"))
sid = out["session_id"]
assert start[start.index("--kind") + 1] == "codex" and start[start.index("--") + 1:] == (
    "--session", sid, "--fake", "--instructions", str(config.HOME / "skills" / pol["skills"]["worker"]))
assert out["project"] == "WEB" and out["branch"] == "ISS-5-fix-it"
assert [m for m, _ in calls] == ["pane.report_metadata", "agent.prompt"]
assert "Load the techichi-worker skill" in calls[-1][1]["text"]
assert c2.execute("SELECT project FROM sessions WHERE session_id = ?", (sid,)).fetchone()[0] == "WEB"
print("ok")
