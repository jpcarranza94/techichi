#!/usr/bin/env python3
"""Recorder, SessionStart: upsert the Session. On /clear, take over the ticket Links of the previous Session in the pane."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import registry


def record(con, data):
    sid, pane = data["session_id"], os.environ.get("HERDR_PANE_ID")
    project = registry.project_for_cwd(data.get("cwd") or ".")
    con.execute("UPDATE sessions SET project = coalesce(project, ?), state = CASE state WHEN 'ended' THEN 'registered' "
                "ELSE state END, ended_at = NULL WHERE session_id = ?", (project, sid))
    if data.get("source") != "clear" or not pane:
        return
    prev = con.execute("SELECT session_id, role FROM sessions WHERE pane_id = ? AND session_id != ? AND state != 'ended' "
                       "ORDER BY coalesce(last_seen, created_at) DESC LIMIT 1", (pane, sid)).fetchone()
    if not prev:
        return
    for (ref,) in con.execute("SELECT ref FROM links WHERE kind = 'ticket' AND session_id = ?", (prev["session_id"],)).fetchall():
        registry.link(con, "ticket", ref, sid, "hook:session_start")
    con.execute("UPDATE sessions SET role = ? WHERE session_id = ?", (prev["role"], sid))
    con.execute("UPDATE sessions SET state = 'ended', ended_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE session_id = ?", (prev["session_id"],))


registry.hook(record)
