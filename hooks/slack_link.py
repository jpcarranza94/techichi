#!/usr/bin/env python3
"""Recorder, PostToolUse slack_send_message: a slack Link with channel:ts. A thread reply links the thread root."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import registry


def record(con, data):
    inp = data.get("tool_input") or {}
    # The result is JSON inside an MCP text block. Strip the escapes and read the two fields.
    text = json.dumps(data.get("tool_response")).replace("\\", "")
    channel = inp.get("channel_id") or (re.findall(r'"channel_id":\s*"(\w+)"', text) or [None])[0]
    ts = inp.get("thread_ts") or (re.findall(r'"message_ts":\s*"(\d+\.\d+)"', text) or [None])[0]
    if channel and ts:
        registry.link(con, "slack", f"{channel}:{ts}", data["session_id"], "hook:slack_link")


registry.hook(record)
