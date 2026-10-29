#!/usr/bin/env python3
"""Recorder, SessionEnd: set the Session to ended."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import registry


def record(con, data):
    con.execute("UPDATE sessions SET state = 'ended', ended_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE session_id = ?", (data["session_id"],))


registry.hook(record)
