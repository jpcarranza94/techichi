"""The registry: the SQLite file, its Sessions and Links, the cursors, and the Recorder runner. See DESIGN.md."""
import json
import os
import re
import subprocess
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

FMT = "%Y-%m-%dT%H:%M:%SZ"
NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"  # the same clock inside SQL


def connect(path=None):
    con = sqlite3.connect(path or config.DB, timeout=5, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 5000")
    schema = (config.HERE / "schema.sql").read_text()
    migrate_sessions(con, schema)
    con.executescript(schema)
    return con


def migrate_sessions(con, schema):
    """SQLite cannot change a CHECK constraint. Rebuild sessions once when its role check lacks a role from schema.sql.

    The order is the SQLite one: create new, copy, drop old, rename new. Renaming the old table first would
    move the foreign keys of links and events to the old name."""
    def stale():
        row = con.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sessions'").fetchone()
        return row and "'planner'" not in row[0]
    if not stale():
        return
    create = re.search(r"CREATE TABLE IF NOT EXISTS sessions \(.*?\n\);", schema, re.S)[0]
    con.execute("BEGIN IMMEDIATE")
    try:
        if stale():  # another writer can migrate first
            con.execute("DROP TABLE IF EXISTS sessions_new")
            con.execute(create.replace("IF NOT EXISTS sessions", "sessions_new"))
            cols = ", ".join(r[1] for r in con.execute("PRAGMA table_info(sessions)"))
            con.execute(f"INSERT INTO sessions_new ({cols}) SELECT {cols} FROM sessions")
            con.execute("DROP TABLE sessions")
            con.execute("ALTER TABLE sessions_new RENAME TO sessions")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# --- Cursors and the clock. A cursor is a named value. A timer cursor holds an ISO time. ----------------------

def iso(t):
    return t.strftime(FMT)


def now(delta=timedelta()):
    return iso(datetime.now(timezone.utc) + delta)


def parse(t):
    return datetime.strptime(t, FMT).replace(tzinfo=timezone.utc)


def cursor(con, name):
    r = con.execute("SELECT value FROM cursors WHERE source = ?", (name,)).fetchone()
    return r["value"] if r else None


def set_cursor(con, name, value):
    con.execute("INSERT INTO cursors (source, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (source) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (name, value, now()))


def drop_cursor(con, name):
    con.execute("DELETE FROM cursors WHERE source = ?", (name,))


def due(con, name, after=timedelta(), at=None):
    """True when the time in cursor `name`, plus `after`, has come. A missing cursor is due.
    A "next" timer uses after = 0. A "last" timer uses after = the interval."""
    t = cursor(con, name)
    return not t or parse(t) + after <= parse(at or now())


def wait(con, name, delta):
    """Set the timer cursor `name` to now plus delta."""
    set_cursor(con, name, now(delta))


# --- Sessions and Links ------------------------------------------------------------------------------------

def project_for_cwd(cwd, pol=None):
    """Project key whose repo owns cwd, or None. A worktree maps to its main checkout through git."""
    try:
        common = subprocess.run([config.GIT, "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        common = ""
    for path in filter(None, [os.path.dirname(common) if common else "", cwd]):
        path = os.path.realpath(path)
        for key, p in (pol or config.policy())["projects"].items():
            repo = os.path.realpath(p["repo"])
            if path == repo or path.startswith(repo + os.sep):
                return key
    return None


def upsert_session(con, sid, cwd=None, pane_id=None, project=None):
    con.execute(f"""INSERT INTO sessions (session_id, role, project, cwd, pane_id, state, last_seen)
                   VALUES (?, 'worker', ?, ?, ?, 'registered', {NOW})
                   ON CONFLICT (session_id) DO UPDATE SET
                     cwd = coalesce(excluded.cwd, cwd), pane_id = coalesce(excluded.pane_id, pane_id),
                     project = coalesce(project, excluded.project), last_seen = excluded.last_seen""",
                (sid, project, cwd, pane_id))


def link(con, kind, ref, sid, created_by):
    con.execute("INSERT OR IGNORE INTO links (kind, ref, session_id, created_by) VALUES (?, ?, ?, ?)",
                (kind, ref, sid, created_by))


def slots_used(con):
    """Spawned workers that hold a slot: a spawner ticket Link and a Session that is registered or live."""
    return con.execute("SELECT count(DISTINCT s.session_id) FROM sessions s JOIN links l USING (session_id) "
                       "WHERE l.kind = 'ticket' AND l.created_by = 'spawner' "
                       "AND s.state IN ('registered','live')").fetchone()[0]


def hook(fn):
    """Run a Recorder. Only in a herdr pane of the policy's session. Never raise, always exit 0.
    No policy means no factory on this machine: exit 0 and write nothing."""
    if not config.POLICY.exists() or not os.environ.get("HERDR_PANE_ID"):
        sys.exit(0)
    try:
        # herdr sets HERDR_SESSION only for a named session.
        if (os.environ.get("HERDR_SESSION") or "default") != config.policy()["factory"]["herdr_session"]:
            sys.exit(0)
        data = json.load(sys.stdin)
        if data.get("session_id") and not data.get("agent_id"):
            con = connect()
            upsert_session(con, data["session_id"], data.get("cwd"), os.environ.get("HERDR_PANE_ID"))
            fn(con, data)
    except Exception as e:
        print(f"techichi {Path(sys.argv[0]).name}: {e}", file=sys.stderr)
    sys.exit(0)
