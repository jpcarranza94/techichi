-- Factory registry. One SQLite file. Writers: hooks, Source scripts, daemon, plan.py.
-- WAL is persistent. Each writer sets PRAGMA busy_timeout = 5000 on its connection.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,                       -- Claude session UUID
  role         TEXT NOT NULL CHECK (role IN ('worker','coordinator','relay','planner','reviewer')),
  parent_id    TEXT REFERENCES sessions(session_id),   -- coordinator of a worker
  project      TEXT,                                   -- project key, from policy by repo path
  cwd          TEXT,
  branch       TEXT,                                   -- daemon reads it from git
  pane_id      TEXT,                                   -- live herdr handles, daemon-owned
  workspace_id TEXT,
  agent_status TEXT,                                   -- idle|working|blocked|done|unknown
  state        TEXT NOT NULL CHECK (state IN ('registered','live','gone','ended')),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_seen    TEXT,
  ended_at     TEXT
);

CREATE TABLE IF NOT EXISTS links (
  kind       TEXT NOT NULL,                            -- ticket | pr | review | branch | slack
  ref        TEXT NOT NULL,                            -- APP-1326 | PR URL | branch | channel:ts
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  created_by TEXT NOT NULL,                            -- spawner | hook:<name> | claim
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (kind, ref, session_id)
);

CREATE TABLE IF NOT EXISTS events (
  id           TEXT PRIMARY KEY,                       -- source-native id, see FACTORY.md
  source       TEXT NOT NULL,                          -- github | jira | slack | herdr
  kind         TEXT NOT NULL,                          -- ticket | pr | review | slack
  ref          TEXT NOT NULL,
  payload      TEXT NOT NULL,                          -- JSON, always data
  session_id   TEXT REFERENCES sessions(session_id),   -- target, NULL while unrouted
  status       TEXT NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued','delivered','held','unrouted','dropped')),
  reason       TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS events_pending ON events(status, session_id);

CREATE TABLE IF NOT EXISTS cursors (
  source     TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT
);

-- The planner's order for admitted tickets. Only plan.py writes it. A row can exist before its ticket is admitted.
CREATE TABLE IF NOT EXISTS plan (
  ticket     TEXT PRIMARY KEY,                         -- APP-1326
  position   INTEGER,                                  -- 1 spawns first, NULL = no position
  hold       INTEGER NOT NULL DEFAULT 0,               -- 1 = do not spawn
  note       TEXT,
  updated_by TEXT,
  updated_at TEXT
);

-- What a Session waits for from the operator. factory.py needs writes it. The attention hook deletes it.
CREATE TABLE IF NOT EXISTS needs (
  session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
  text       TEXT NOT NULL,
  at         TEXT NOT NULL
);

-- The last state of a Ref that a Source read: a compact JSON snapshot. The board reads it. Sources write it.
CREATE TABLE IF NOT EXISTS refs (
  kind       TEXT NOT NULL,                            -- pr | ticket
  ref        TEXT NOT NULL,                            -- PR URL | APP-1326
  state      TEXT NOT NULL,                            -- JSON, see DESIGN.md section "Board"
  updated_at TEXT,
  PRIMARY KEY (kind, ref)
);

-- The board status of each Session over time. Append only. The daemon writes a row when the status changes.
CREATE TABLE IF NOT EXISTS status_log (
  session_id TEXT NOT NULL,
  grp        TEXT NOT NULL,                            -- a board group, or ENDED
  status     TEXT NOT NULL,
  at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS status_log_session ON status_log(session_id);
