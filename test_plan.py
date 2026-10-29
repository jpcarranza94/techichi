"""Check for the sessions migration and plan.py. Scratch databases, a stub herdr. Run: python3 test_plan.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import config  # noqa: E402
import contextlib
import io
import os
import sqlite3
import tempfile
from pathlib import Path

import plan
import registry

d = Path(tempfile.mkdtemp())

# --- Migration: a database with the old role check gets the planner role. Rows and foreign keys survive. ---
old = config.HERE.joinpath("schema.sql").read_text().replace("'relay','planner'", "'relay'")
db = d / "old.db"
o = sqlite3.connect(db)
o.executescript(old.split("-- The planner's order")[0])  # the old schema had no plan table
o.execute("INSERT INTO sessions (session_id, role, state, pane_id) VALUES ('s1', 'worker', 'live', 'wA:p1')")
o.execute("INSERT INTO links (kind, ref, session_id, created_by) VALUES ('ticket', 'APP-1', 's1', 'spawner')")
o.execute("INSERT INTO events (id, source, kind, ref, payload, session_id) VALUES ('e1', 'jira', 'ticket', 'APP-1', '{}', 's1')")
o.commit()
try:
    o.execute("INSERT INTO sessions (session_id, role, state) VALUES ('x', 'planner', 'live')")
    raise AssertionError("old schema took a planner")
except sqlite3.IntegrityError:
    pass
o.close()

con = registry.connect(db)
assert "'planner'" in con.execute("SELECT sql FROM sqlite_master WHERE name = 'sessions'").fetchone()[0]
assert [tuple(r) for r in con.execute("SELECT session_id, role, state, pane_id FROM sessions")] == [("s1", "worker", "live", "wA:p1")]
assert con.execute("SELECT count(*) FROM links").fetchone()[0] == 1 and con.execute("SELECT count(*) FROM events").fetchone()[0] == 1
for t in ("links", "events"):  # the foreign keys still name sessions, not a renamed table
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()[0]
    assert "REFERENCES sessions(session_id)" in sql and "sessions_new" not in sql and "sessions_old" not in sql, sql
assert not con.execute("PRAGMA foreign_key_check").fetchall()
assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
con.execute("INSERT INTO sessions (session_id, role, state) VALUES ('p1', 'planner', 'live')")
assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} >= {"sessions", "plan", "links", "events"}
registry.connect(db)  # a second connect does not migrate again
assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2

# --- plan.py ---
con = registry.connect(d / "plan.db")
plan.order(con, ["app-3", "APP-1", "WEB-2"])
assert [tuple(r) for r in con.execute("SELECT ticket, position, hold FROM plan ORDER BY position")] == [
    ("APP-3", 1, 0), ("APP-1", 2, 0), ("WEB-2", 3, 0)]
plan.order(con, ["WEB-2", "APP-9"])  # rows for other keys stay
assert dict(con.execute("SELECT ticket, position FROM plan").fetchall()) == {"APP-3": 1, "APP-1": 2, "WEB-2": 1, "APP-9": 2}
try:
    plan.order(con, ["APP-1", "app-1"])
    raise AssertionError("duplicate key accepted")
except SystemExit:
    pass

plan.hold(con, "app-1", "waits for the PM decision on scope", True)
plan.hold(con, "WEB-7", "not in this sprint", True)  # a row before the ticket is admitted
row = con.execute("SELECT * FROM plan WHERE ticket = 'APP-1'").fetchone()
assert (row["hold"], row["note"], row["position"]) == (1, "waits for the PM decision on scope", 2)
plan.hold(con, "APP-1", None, False)  # release keeps the note
row = con.execute("SELECT * FROM plan WHERE ticket = 'APP-1'").fetchone()
assert (row["hold"], row["note"]) == (0, "waits for the PM decision on scope") and row["updated_at"]

# show: waiting admitted tickets with reason, plan rows, active spawned count against the cap.
con.execute("INSERT INTO events (id, source, kind, ref, payload, status, reason) VALUES "
            "('j1', 'jira', 'ticket', 'APP-9', '{\"admitted\": true, \"url\": \"https://x/APP-9\"}', 'queued', 'capacity'), "
            "('j2', 'jira', 'ticket', 'APP-8', '{\"admitted\": false}', 'queued', NULL)")
con.execute("INSERT INTO sessions (session_id, role, state) VALUES ('w1', 'worker', 'live')")
registry.link(con, "ticket", "APP-3", "w1", "spawner")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    plan.show(con, {"spawn": {"max_active": 10}})
text = out.getvalue()
assert "Spawned workers: 1 active of 10." in text and "APP-9" in text and "capacity" in text and "APP-8" not in text.split("Plan rows")[0]
assert "WEB-7" in text and "hold" in text and "not in this sprint" in text

# claim-role: a stub herdr returns the session of the pane.
stub = d / "herdr"
stub.write_text('#!/bin/sh\necho \'{"result":{"agent":{"agent_session":{"value":"plan-sid"}}}}\'\n')
stub.chmod(0o755)
config.HERDR = str(stub)
os.environ["HERDR_PANE_ID"] = "wP:p1"
with contextlib.redirect_stdout(io.StringIO()):
    plan.claim_role(con)
assert tuple(con.execute("SELECT role, pane_id FROM sessions WHERE session_id = 'plan-sid'").fetchone()) == ("planner", "wP:p1")
assert con.execute("SELECT 1 FROM links WHERE kind = 'inbox' AND ref = 'operator' AND session_id = 'plan-sid'").fetchone()
assert con.execute("SELECT updated_by FROM plan WHERE ticket = 'APP-3'").fetchone()[0]  # writer recorded
print("ok")
