"""Check for the five Recorders: pipe hook JSON into each script. Run: python3 test_hooks.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent / "hooks"
d = tempfile.mkdtemp()
DB = str(testenv.HOME / "registry.db")


def fire(script, data, session="default", pane="wX:p1"):
    env = dict(os.environ, HERDR_SESSION=session, HERDR_PANE_ID=pane)
    stdin = data if isinstance(data, str) else json.dumps(data)
    r = subprocess.run(["python3", HOOKS / script], input=stdin, env=env, capture_output=True, text=True, timeout=5)
    assert r.returncode == 0, r


def q(sql, *a):
    return sqlite3.connect(DB).execute(sql, a).fetchall()


# Outside the policy's herdr session nothing is written.
fire("session_start.py", {"session_id": "s0", "cwd": d, "source": "startup"}, session="other")
assert not os.path.exists(DB)

fire("session_start.py", {"session_id": "s1", "cwd": d, "source": "startup"})
assert q("SELECT role, state, pane_id FROM sessions") == [("worker", "registered", "wX:p1")]

fire("ticket_link.py", {"session_id": "s1", "tool_name": "Skill",
                        "tool_input": {"skill": "skills:ship-ticket", "args": "APP-1326 in place"}})
fire("ticket_link.py", {"session_id": "s1", "tool_input": {"skill": "other", "args": "APP-9"}})
fire("pr_link.py", {"session_id": "s1", "tool_input": {"command": "cd x && gh pr create --fill"},
                    "tool_response": {"stdout": "https://github.com/acme/app/pull/563\n"}})
fire("pr_link.py", {"session_id": "s1", "tool_input": {"command": "gh pr view 1"},
                    "tool_response": {"stdout": "https://github.com/acme/app/pull/1"}})
slack_out = json.dumps({"message_link": "https://x.slack.com/archives/C0B/p1791240000000001",
                        "message_context": {"message_ts": "1791240000.000001", "channel_id": "C0B"}})
fire("slack_link.py", {"session_id": "s1", "tool_input": {"channel_id": "C0B", "message": "hi"},
                       "tool_response": [{"type": "text", "text": slack_out}]})
fire("slack_link.py", {"session_id": "s1", "tool_input": {"channel_id": "C0B", "thread_ts": "1.2"},
                       "tool_response": [{"type": "text", "text": slack_out}]})
assert sorted(q("SELECT kind, ref FROM links WHERE session_id = 's1'")) == [
    ("pr", "https://github.com/acme/app/pull/563"), ("slack", "C0B:1.2"),
    ("slack", "C0B:1791240000.000001"), ("ticket", "APP-1326")]

# /clear in the same pane: s2 takes the ticket Link, s1 ends.
fire("session_start.py", {"session_id": "s2", "cwd": d, "source": "clear"})
assert q("SELECT ref FROM links WHERE session_id = 's2'") == [("APP-1326",)]
assert q("SELECT state FROM sessions WHERE session_id = 's1'") == [("ended",)]

fire("session_end.py", {"session_id": "s2", "hook_event_name": "SessionEnd"})
assert q("SELECT state FROM sessions WHERE session_id = 's2'") == [("ended",)]

# Bad input never fails the hook.
fire("pr_link.py", "not json")
print("ok")
