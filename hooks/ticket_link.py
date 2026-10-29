#!/usr/bin/env python3
"""Recorder, PostToolUse Skill: a ticket Link when a start skill of the policy runs for a key."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import config
import registry


def record(con, data):
    inp = data.get("tool_input") or {}
    starts = {p["start"]["skill"] for p in config.policy()["projects"].values()}
    if inp.get("skill", "").split(":")[-1] not in starts:
        return
    for key in dict.fromkeys(re.findall(r"\b[A-Z][A-Z0-9]+-\d+\b", str(inp.get("args", "")))):
        registry.link(con, "ticket", key, data["session_id"], "hook:ticket_link")


registry.hook(record)
