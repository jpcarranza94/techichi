"""Shared code for the factory: config, registry connection and policy."""
import json
import os
import re
import shutil
import subprocess
import sys
import sqlite3
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent  # the code: schema.sql and the skill templates
# The config folder: policy.toml, registry.db and the rendered skills. Hooks get no env, so the default matters.
HOME = Path(os.path.expanduser(os.environ.get("TECHICHI_HOME", "~/.config/techichi"))).resolve()
DB = HOME / "registry.db"
POLICY = HOME / "policy.toml"
# launchd gives no PATH. TECHICHI_HERDR, TECHICHI_GIT and TECHICHI_GH override the lookup.
HERDR, GIT, GH = (os.environ.get(f"TECHICHI_{n.upper()}") or shutil.which(n) or n for n in ("herdr", "git", "gh"))


def connect(path=None):
    con = sqlite3.connect(path or DB, timeout=5, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 5000")
    schema = (HERE / "schema.sql").read_text()
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


def policy(path=None):
    pol = tomllib.loads(Path(path or POLICY).read_text())
    for p in pol.get("projects", {}).values():
        p["repo"] = os.path.expanduser(p["repo"])
    return pol


def factory(pol=None):
    """The [factory] table of the policy with its defaults. Paths come back expanded."""
    f = {"herdr_session": "default", "metadata_source": "techichi", "inbox_ref": "operator",
         "worktrees": HOME / "worktrees", "helper_cwd": HOME, "relay_cwd": HOME / "relay",
         **(policy() if pol is None else pol).get("factory", {})}
    for k in ("worktrees", "helper_cwd", "relay_cwd"):
        f[k] = Path(os.path.expanduser(f[k]))
    return f


def skill(role, pol=None):
    """The skill name for a role: worker, planner or relay. Prompts name it, and install.py renders it."""
    return (policy() if pol is None else pol).get("skills", {}).get(role, f"techichi-{role}")


def herdr_cmd(pol=None):
    return [HERDR, "--session", factory(pol)["herdr_session"]]


def socket_path(pol=None):
    """The herdr socket of the session. herdr keeps the default session at the top of its folder."""
    name = factory(pol)["herdr_session"]
    return Path.home() / ".config/herdr" / ("herdr.sock" if name == "default" else f"sessions/{name}/herdr.sock")


def project_for_cwd(cwd, pol=None):
    """Project key whose repo owns cwd, or None. A worktree maps to its main checkout through git."""
    try:
        common = subprocess.run([GIT, "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        common = ""
    for path in filter(None, [os.path.dirname(common) if common else "", cwd]):
        path = os.path.realpath(path)
        for key, p in (pol or policy())["projects"].items():
            repo = os.path.realpath(p["repo"])
            if path == repo or path.startswith(repo + os.sep):
                return key
    return None


def upsert_session(con, sid, cwd=None, pane_id=None, project=None):
    con.execute("""INSERT INTO sessions (session_id, role, project, cwd, pane_id, state, last_seen)
                   VALUES (?, 'worker', ?, ?, ?, 'registered', strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                   ON CONFLICT (session_id) DO UPDATE SET
                     cwd = coalesce(excluded.cwd, cwd), pane_id = coalesce(excluded.pane_id, pane_id),
                     project = coalesce(project, excluded.project), last_seen = excluded.last_seen""",
                (sid, project, cwd, pane_id))


def link(con, kind, ref, sid, created_by):
    con.execute("INSERT OR IGNORE INTO links (kind, ref, session_id, created_by) VALUES (?, ?, ?, ?)",
                (kind, ref, sid, created_by))


def hook(fn):
    """Run a Recorder. Only in a herdr pane of the policy's session. Never raise, always exit 0.
    No policy means no factory on this machine: exit 0 and write nothing."""
    if not POLICY.exists() or not os.environ.get("HERDR_PANE_ID"):
        sys.exit(0)
    try:
        # herdr sets HERDR_SESSION only for a named session.
        if (os.environ.get("HERDR_SESSION") or "default") != factory()["herdr_session"]:
            sys.exit(0)
        data = json.load(sys.stdin)
        if data.get("session_id") and not data.get("agent_id"):
            con = connect()
            upsert_session(con, data["session_id"], data.get("cwd"), os.environ.get("HERDR_PANE_ID"))
            fn(con, data)
    except Exception as e:
        print(f"techichi {Path(sys.argv[0]).name}: {e}", file=sys.stderr)
    sys.exit(0)
