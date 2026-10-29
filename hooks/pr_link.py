#!/usr/bin/env python3
"""Recorder, PostToolUse Bash: a pr Link for the URL that `gh pr create` prints."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import registry

# Same command match as ~/.claude/hooks/ci-monitor-on-push-or-pr.sh.
CREATE = re.compile(r"(^|[\s&|;])gh\s+pr\s+create(\s|$)")
URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def record(con, data):
    if not CREATE.search((data.get("tool_input") or {}).get("command", "")):
        return
    resp = data.get("tool_response")
    out = resp.get("stdout", "") if isinstance(resp, dict) else str(resp)
    for url in dict.fromkeys(URL.findall(out)):
        registry.link(con, "pr", url, data["session_id"], "hook:pr_link")


registry.hook(record)
