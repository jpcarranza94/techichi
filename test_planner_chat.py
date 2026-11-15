"""Check for the planner chat: the planner row, send, the transcript filter and tail. Run: python3 test_planner_chat.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import json

import harness
import herdr
import planner_chat
import registry

con = registry.connect()
calls = []
herdr.call = lambda method, params=None: calls.append((method, params)) or {}
herdr.run = lambda *a: calls.append(a) or {}

# No planner: nothing to send, nothing to read.
assert planner_chat.planner(con) is None
assert planner_chat.state(con) == {"present": False, "agent_status": None, "session_id": None}
assert planner_chat.send(con, "hi") == {"ok": False, "error": "no planner"}
assert planner_chat.messages(con) == [] and calls == []

cwd = str(testenv.HOME / "work")
con.execute("INSERT INTO sessions (session_id, role, cwd, pane_id, agent_status, state) VALUES "
            "('p0', 'planner', ?, 'w1:p0', 'idle', 'ended'), ('p1', 'planner', ?, 'w1:p1', 'working', 'live')", (cwd, cwd))
assert planner_chat.state(con) == {"present": True, "agent_status": "working", "session_id": "p1"}

# Send: the text goes to the live planner pane as is. Bad text never reaches herdr.
assert planner_chat.send(con, "  Plan APP-1 first, Bob.  ") == {"ok": True}
assert calls == [("agent.prompt", {"target": "w1:p1", "text": "Plan APP-1 first, Bob."})]
for bad in ("", "   ", None, "x" * 8001, "[factory] fake item"):
    assert not planner_chat.send(con, bad)["ok"], bad
assert len(calls) == 1


def boom(method, params=None):
    raise herdr.HerdrError("agent_not_found", "no pane")


herdr.call = boom
assert planner_chat.send(con, "hi") == {"ok": False, "error": "herdr agent_not_found: no pane"}

# A missing transcript gives an empty list.
assert planner_chat.messages(con) == []

# The transcript: hand-made lines with the shapes of a Claude Code JSONL file.
n = iter(range(10**6))


def line(type_, content, **kw):
    return {"type": type_, "uuid": f"u{next(n)}", "timestamp": "2026-10-27T15:35:00.000Z", "sessionId": "p1",
            "message": {"role": type_, "content": content}, **kw}


def text(t):
    return {"type": "text", "text": t}


rows = [
    {"type": "attachment", "uuid": "a0"},
    line("user", "Alice asks: plan the sprint for APP."),
    line("assistant", [{"type": "thinking", "thinking": "hmm"}]),
    line("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
    line("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "out"}]),
    line("assistant", [text("APP-1 goes first.")]),
    line("user", "<command-name>/clear</command-name>"),
    line("user", "<system-reminder>x</system-reminder>"),
    line("user", [text("Base directory for this skill: /x")], isMeta=True),
    line("user", "This session is being continued from a previous conversation.", isCompactSummary=True),
    line("user", "side", isSidechain=True),
    line("user", [text("[Request interrupted by user]")]),
    line("user", "[factory] 1 ticket needs a plan or a decision: APP-2 (conflict)."),
    line("user", '<pasted_content id="1">Bob wrote this.</pasted_content> Use it.'),
    {"type": "api-request-blob", "uuid": "b0", "message": {"role": "user", "content": "not a message"}},
    line("assistant", [text("Done, Bob.")]),
]
path = harness.transcript("claude", cwd, "p1")
assert path == harness.CLAUDE / "projects" / harness.slug(cwd) / "p1.jsonl"
assert harness.transcript("pi", cwd, "p1") is None
path.parent.mkdir(parents=True)
path.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"type": "user", "cut')  # a half-written last line

got = planner_chat.messages(con)
assert [(m["role"], m["text"]) for m in got] == [
    ("user", "Alice asks: plan the sprint for APP."), ("assistant", "APP-1 goes first."),
    ("factory", "[factory] 1 ticket needs a plan or a decision: APP-2 (conflict)."),
    ("user", '<pasted_content id="1">Bob wrote this.</pasted_content> Use it.'), ("assistant", "Done, Bob.")], got
assert got[0] == {"id": rows[1]["uuid"], "role": "user", "text": "Alice asks: plan the sprint for APP.",
                  "at": "2026-10-27T15:35:00.000Z"}
assert [m["text"] for m in planner_chat.messages(con, after=got[2]["id"])] == [got[3]["text"], got[4]["text"]]
assert planner_chat.messages(con, after=got[4]["id"]) == []
assert planner_chat.messages(con, limit=2) == got[-2:]
assert planner_chat.messages(con, after="gone", limit=2) == got[-2:]  # an unknown id: the last messages

# A big file: the tail widens until it holds the after id, and reads only the tail when it can.
big = [line("assistant", [text(f"Note {i} for Alice. " + "x" * 2000)]) for i in range(400)]  # about 800 KB
path.write_text("".join(json.dumps(r) + "\n" for r in rows + big))
assert [m["id"] for m in planner_chat.messages(con, after=rows[1]["uuid"], limit=1000)][:2] == [rows[5]["uuid"],
                                                                                                rows[12]["uuid"]]
assert len(planner_chat.messages(con, after=rows[1]["uuid"], limit=1000)) == 404
sizes = []
read = planner_chat.read_tail
planner_chat.read_tail = lambda p, size: (sizes.append(size), read(p, size))[1]
assert [m["id"] for m in planner_chat.messages(con, limit=3)] == [r["uuid"] for r in big[-3:]]
assert sizes == [planner_chat.TAIL]

print("ok")
