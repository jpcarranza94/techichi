#!/usr/bin/env python3
"""Stop and UserPromptSubmit hook: mark the herdr pane while its agent waits for the operator.
A reply waits for the operator when one of its lines starts with "Needs you:". The next prompt clears the mark.
Display only: herdr keeps the semantic state. Not a Recorder: it writes no Link."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import herdr
import registry

NEED = re.compile(r"^\W*needs you\b\W*(.*)$", re.I | re.M)


def mark(con, data):
    m = data.get("hook_event_name") == "Stop" and NEED.search(data.get("last_assistant_message") or "")
    herdr.report_attention(os.environ["HERDR_PANE_ID"], m.group(1).strip(" *_") if m else None)


registry.hook(mark)
