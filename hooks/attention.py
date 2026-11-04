#!/usr/bin/env python3
"""UserPromptSubmit hook: the operator wrote in this pane, so clear the flag that `factory.py needs` set.
A "[factory]" item prompt is not the operator's answer: the flag stays. Any other hook event does nothing.
Display only: herdr keeps the semantic state. Not a Recorder: it writes no Link."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import herdr
import registry


def clear(con, data):
    if data.get("hook_event_name") != "UserPromptSubmit" or (data.get("prompt") or "").lstrip().startswith("[factory]"):
        return
    pane = os.environ["HERDR_PANE_ID"]
    # Every Session of this pane: a /clear gives the pane a new session id.
    gone = con.execute("DELETE FROM needs WHERE session_id = ? OR session_id IN "
                       "(SELECT session_id FROM sessions WHERE pane_id = ?)", (data["session_id"], pane)).rowcount
    if gone:
        herdr.report_attention(pane, None)


registry.hook(clear)
