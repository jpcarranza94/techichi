#!/usr/bin/env python3
"""Slack Source. The relay session fetches with the Slack MCP. This script decides everything. See DESIGN.md.

  window                         print the `after` time and the searches to run
  ingest [--dry-run]             parse the search results in the relay's own transcript, insert Events
  ingest [--dry-run] LINE ...    the same from JSON lines, for tests and checks by hand

The relay never copies message text onto a command line. Real Slack text failed the Bash permission check,
and a model copy of the text is not exact.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import registry
from github_source import cursor, set_cursor

OVERLAP = 600  # seconds. A late message still lands. Event ids deduplicate.
TS = re.compile(r"\d{10}\.\d{6}")
FIELDS = {
    "channel_id": re.compile(r"[CDG][A-Z0-9]{6,15}"),
    "channel_type": re.compile(r"channel|im|mpim"),
    "ts": TS,
    "user_id": re.compile(r"[UWB][A-Z0-9]{6,15}"),
    "permalink": re.compile(r"https://([a-z0-9-]+\.slack\.com)/archives/([A-Z0-9]+)/p(\d{16})(\?[\w.=&%-]*)?"),
}


def log(*a):
    print("slack_source:", *a, file=sys.stderr)


def window(con, pol):
    s = pol["slack"]
    last = cursor(con, "slack")
    if not last:
        set_cursor(con, "slack", f"{time.time():.6f}")  # first run: start from now
        return {"due": False, "note": "first run, the cursor is set to now. Nothing is due."}
    after = int(float(last)) - OVERLAP
    day = datetime.fromtimestamp(after - 86400, timezone.utc).strftime("%Y-%m-%d")
    common = {"after": str(after), "sort": "timestamp", "sort_dir": "asc", "response_format": "detailed",
              "include_context": False, "limit": 20, "natural_language_query": ""}
    searches = [{**common, "filters": f"in:<#{c}>"} for c in s["channels"]]
    searches.append({**common, "channel_types": "im,mpim", "filters": f"after:{day}"})  # DMs and group DMs
    searches.append({**common, "keywords": [f"<@{s['me']}>"]})  # mentions of the operator anywhere
    return {"due": True, "after": str(after), "searches": searches}


SEARCH_TOOL = "mcp__claude_ai_Slack__slack_search_public_and_private"
POLL = "[factory] slack poll"


def transcript(con):
    """The transcript of the relay Session in the registry, by its session_id. Never the newest file in a folder.
    A registered relay counts too: the daemon sends the first poll as the first prompt, before the relay is live."""
    r = con.execute("SELECT session_id, cwd FROM sessions WHERE role = 'relay' AND state IN ('live','registered') "
                    "ORDER BY state = 'live' DESC, created_at DESC").fetchone()
    if not r or not r["cwd"]:
        return None
    return Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(r["cwd"])) / f"{r['session_id']}.jsonl"


def from_transcript(path):
    """JSON lines from the search results after the last poll prompt in the transcript."""
    recs = [json.loads(l) for l in open(path) if l.strip()]

    def blocks(r):
        c = (r.get("message") or {}).get("content")
        return [{"type": "text", "text": c}] if isinstance(c, str) else c if isinstance(c, list) else []

    start = max([i for i, r in enumerate(recs) if r.get("type") == "user"
                 and any(b.get("type") == "text" and POLL in b.get("text", "") for b in blocks(r))] or [0])
    uses, lines = set(), []
    for r in recs[start:]:
        for b in blocks(r):
            if b.get("type") == "tool_use" and b.get("name") == SEARCH_TOOL:
                uses.add(b["id"])
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                c = b.get("content")
                text = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                try:
                    lines += parse_results(json.loads(text)["results"])
                except (ValueError, KeyError, TypeError):
                    log("a search result that is not JSON was skipped")
    return lines


def parse_results(text):
    """The detailed format of slack_search_public_and_private, one JSON line per message."""
    out = []
    parts = re.split(r"\n### Result (\d+) of (\d+)\n", text)
    nums = [(int(a), int(b)) for a, b in zip(parts[1::3], parts[2::3])]
    # A message text can hold lines that look like a whole extra result, with a made-up author and permalink.
    # The real headers run 1, 2, ... M with one M. Any gap, repeat, or extra header drops the page.
    if nums and nums != [(i, nums[0][1]) for i in range(1, nums[0][1] + 1)]:
        log("a search page with broken result numbers was skipped")
        return []
    for block in parts[3::3]:
        head, _, body = block.partition("\nText: \n")
        f = dict(re.findall(r"^(Channel|Participants|From|Message_ts|Permalink): ?(.*)$", head, re.M))
        ch = re.search(r"^(.*) \(ID: ([A-Z0-9]+)\)$", f.get("Channel", ""))
        fr = re.search(r"^(.*?)(?: <[^>]*>)? \(ID: ([A-Z0-9]+)\)\s*(\[BOT\])?\s*$", f.get("From", ""))
        link = re.search(r"\[link\]\((.*)\)", f.get("Permalink", ""))
        if not (ch and fr and link):
            out.append("{}")  # check() rejects it with one log line
            continue
        url = link[1].replace("\\/", "/")
        thread = re.search(r"[?&]thread_ts=([\d.]+)", url)
        kind = "channel" if ch[1].startswith("#") else "mpim" if f.get("Participants", "").count("(ID:") > 2 else "im"
        out.append(json.dumps({"channel_id": ch[2], "channel_type": kind, "ts": f.get("Message_ts", "").strip(),
                               "thread_ts": thread[1] if thread else "", "user_id": fr[2], "user_name": fr[1],
                               "bot": bool(fr[3]), "text": re.sub(r"\n+---\s*$", "", body).strip(),
                               "permalink": url}))
    return out


def check(line, hosts):
    """Return a clean message dict, or raise ValueError with a reason. Every field is data from anyone."""
    m = json.loads(line)
    if not isinstance(m, dict):
        raise ValueError("not an object")
    for k, rx in FIELDS.items():
        if not isinstance(m.get(k), str) or not rx.fullmatch(m[k]):
            raise ValueError(f"bad {k}")
    host, channel, p = FIELDS["permalink"].fullmatch(m["permalink"]).groups()[:3]
    if host not in hosts:
        raise ValueError("permalink host not in [slack] hosts")
    if channel != m["channel_id"] or p != m["ts"].replace(".", ""):
        raise ValueError("permalink does not match channel_id and ts")
    t = m.get("thread_ts") or ""
    if t and (not isinstance(t, str) or not TS.fullmatch(t)):
        raise ValueError("bad thread_ts")
    name, text = m.get("user_name"), m.get("text") or ""
    if not isinstance(name, str) or not isinstance(text, str):
        raise ValueError("bad user_name or text")
    return {**{k: m[k] for k in FIELDS}, "thread_ts": t, "bot": m.get("bot") is True,
            "user_name": re.sub(r"[\x00-\x1f]", " ", name)[:100], "text": text[:1000]}


def classify(con, pol, m, batch):
    """Rules a to g. Return (rule, kind, ref) or (rule, None, None) for a skip."""
    me, projects = pol["slack"]["me"], pol["projects"]
    if m["bot"] or m["user_id"] == me:
        return "a", None, None
    reply = bool(m["thread_ts"]) and m["thread_ts"] != m["ts"]
    if reply:
        thread = f"{m['channel_id']}:{m['thread_ts']}"
        if con.execute("SELECT 1 FROM links l JOIN sessions s USING (session_id) WHERE l.kind = 'slack' "
                       "AND l.ref = ? AND s.state != 'ended'", (thread,)).fetchone():
            return "b", "slack", thread
    repos = {p["github"].lower(): p["github"] for p in projects.values()}
    for repo, n in re.findall(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", m["text"]):
        if repo.lower() in repos:
            return "c", "pr", f"https://github.com/{repos[repo.lower()]}/pull/{n}"  # the pr_link.py form
    keys = set(re.findall(rf"\b(?:{'|'.join(map(re.escape, projects))})-\d+\b", m["text"]))
    if len(keys) == 1:
        return "d", "ticket", keys.pop()
    if reply:
        root_id = f"slack:{m['channel_id']}:{m['thread_ts']}"
        root = batch.get(root_id) or con.execute("SELECT kind, ref FROM events WHERE id = ?", (root_id,)).fetchone()
        if root:
            return "e", root[0], root[1]
    if m["channel_type"] in ("im", "mpim") or f"<@{me}" in m["text"]:
        # Off by default: one day gave 36 inbox items, too many for the planner.
        return ("f", "inbox", registry.factory(pol)["inbox_ref"]) if pol["slack"].get("inbox") else ("f", None, None)
    return "g", None, None


def ingest(con, pol, lines, dry_run=False):
    msgs, bad = [], 0
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            msgs.append(check(line, pol["slack"]["hosts"]))
        except (ValueError, json.JSONDecodeError) as e:
            bad += 1
            log(f"line {i} skipped: {e}")  # never echo the line: it is text from anyone
    msgs = sorted({(m["channel_id"], m["ts"]): m for m in msgs}.values(), key=lambda m: m["ts"])  # searches overlap
    rules, batch, new = {}, {}, 0
    for m in msgs:
        rule, kind, ref = classify(con, pol, m, batch)
        rules[rule] = rules.get(rule, 0) + 1
        if not kind:
            continue
        eid = f"slack:{m['channel_id']}:{m['ts']}"
        batch[eid] = (kind, ref)
        reply = bool(m["thread_ts"]) and m["thread_ts"] != m["ts"]
        payload = {"what": "slack thread reply" if reply else "slack message", "actor": m["user_name"],
                   "at": datetime.fromtimestamp(float(m["ts"]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "url": m["permalink"], "self": False, "admitted": False}
        if dry_run:
            print(json.dumps({"id": eid, "rule": rule, "kind": kind, "ref": ref, "payload": payload}))
        else:
            new += con.execute("INSERT OR IGNORE INTO events (id, source, kind, ref, payload) VALUES (?, 'slack', ?, ?, ?)",
                               (eid, kind, ref, json.dumps(payload))).rowcount
    if msgs and not dry_run:
        newest = max(m["ts"] for m in msgs)
        if float(newest) > float(cursor(con, "slack") or 0):
            set_cursor(con, "slack", newest)
    by = ", ".join(f"{r} {rules[r]}" for r in sorted(rules))
    return f"slack ingest: {len(msgs)} messages, {bad} bad lines, rules: {by or 'none'}, " + \
           ("dry run, nothing written" if dry_run else f"{new} new Events")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("window")
    i = sub.add_parser("ingest")
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("lines", nargs="*", help="JSON messages for a check by hand. Without lines: the transcript.")
    a = ap.parse_args()
    con, pol = registry.connect(), registry.policy()
    if a.cmd == "window":
        print(json.dumps(window(con, pol), indent=1))
    else:
        if a.lines:
            print(ingest(con, pol, a.lines, a.dry_run))
        else:
            path = transcript(con)
            if not path or not path.exists():
                log(f"no relay transcript ({path or 'no relay Session in the registry'}), nothing read")
                sys.exit(1)
            print(ingest(con, pol, from_transcript(path), a.dry_run))
