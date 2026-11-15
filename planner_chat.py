"""Planner chat: the bridge between a web pane and the one planner Session. See DESIGN.md "Planner chat".

send() prompts the planner pane, so the conversation goes on from the terminal or from the page.
messages() reads the planner's conversation back from its transcript. No HTTP code here.
"""
import json
import os

import config
import harness
import herdr

MAX_TEXT = 8000
TAIL = 256 * 1024  # the first read from the end of the transcript. It doubles until it holds enough.


def planner(con):
    """The planner Session that is not ended, or None. A live one wins over one that is still starting."""
    return con.execute("SELECT * FROM sessions WHERE role = 'planner' AND state != 'ended' "
                       "ORDER BY state = 'live' DESC, last_seen DESC").fetchone()


def state(con):
    p = planner(con)
    return {"present": p is not None, "agent_status": p and p["agent_status"], "session_id": p and p["session_id"]}


def send(con, text):
    """Give the operator's text to the planner pane as a normal prompt. Never raises."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    if len(text) > MAX_TEXT:
        return {"ok": False, "error": f"text over {MAX_TEXT} characters"}
    if text.startswith("[factory]"):  # the attention hook would take it for an item, not the operator's answer
        return {"ok": False, "error": "text must not start with [factory]"}
    try:
        p = planner(con)
        if not p or not p["pane_id"]:
            return {"ok": False, "error": "no planner"}
        herdr.prompt(p["pane_id"], text)
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}
    return {"ok": True}


def message(d):
    """One transcript line as a chat message, or None for anything that is not operator or assistant text."""
    if d.get("type") not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta") \
            or d.get("isCompactSummary"):
        return None
    content = (d.get("message") or {}).get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list) or any(b.get("type") == "tool_result" for b in content):
        return None
    text = "\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text")).strip()
    role = d["type"]
    if role == "user":
        # A tag is a command, its output, a task notice, or a reminder. A paste is the operator's own text.
        if (text.startswith("<") and not text.startswith("<pasted_content")) or text.startswith("[Request interrupted"):
            return None
        if text.startswith("[factory]"):
            role = "factory"
    if not text:
        return None
    return {"id": d.get("uuid"), "role": role, "text": text, "at": d.get("timestamp")}


def read_tail(path, size):
    """The messages in the last size bytes of the file, and whether that was the whole file."""
    with open(path, "rb") as f:
        end = f.seek(0, os.SEEK_END)
        start = max(0, end - size)
        f.seek(start)
        lines = f.read().split(b"\n")
    if start:
        lines = lines[1:]  # the first line is cut
    out = []
    for line in lines:
        try:
            m = message(json.loads(line))
        except ValueError:
            continue  # a cut or half-written line
        if m:
            out.append(m)
    return out, start == 0


def messages(con, after=None, limit=50):
    """The planner's recent conversation, oldest first. With after, only the messages after that id.
    An unknown after id (a new session after /clear) gives the last limit messages."""
    limit = max(1, limit)
    p = planner(con)
    path = p and harness.transcript(config.policy()["factory"]["harness"], p["cwd"], p["session_id"])
    if not path or not path.exists():
        return []
    size = TAIL
    while True:
        out, whole = read_tail(path, size)
        ids = [m["id"] for m in out]
        if after in ids:
            return out[ids.index(after) + 1:][-limit:]
        if whole or len(out) >= limit:  # limit messages after the window start: also the last limit after `after`
            return out[-limit:]
        size *= 2
