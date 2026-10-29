#!/usr/bin/env python3
"""Jira Source: sprint admissions and updates on linked tickets become Events. Read only. See DESIGN.md."""
import argparse
import base64
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config
import registry
from registry import cursor, iso, now, set_cursor  # noqa: F401  (the Source tests use them)

OVERLAP = timedelta(minutes=10)
_auth, _me = None, None


def get(pol, path, **query):
    """GET on the Jira API with Basic auth. The token stays in this function. Errors name only the path."""
    global _auth
    j = pol["jira"]
    if _auth is None:
        with open(os.path.expanduser(j["token_file"])) as f:
            _auth = base64.b64encode(f"{j['email']}:{f.read().strip()}".encode()).decode()
    url = j["api"] + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + _auth, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        err = f"HTTP {e.code}"
    except Exception as e:
        err = type(e).__name__
    # Raised outside the except block, so no __context__ keeps the request or the server reply.
    raise RuntimeError(f"jira GET {path}: {err}")


def search(pol, jql, fields, expand=None):
    # ponytail: one page of 100 issues. Admission and linked tickets stay far below that.
    q = {"jql": jql, "fields": fields, "maxResults": 100, **({"expand": expand} if expand else {})}
    return get(pol, "/search/jql", **q).get("issues", [])


def me(pol):
    global _me
    if _me is None:
        _me = get(pol, "/myself")["accountId"]
    return _me


def slug(summary):
    return "-".join(re.findall(r"[a-z0-9]+", summary.lower().encode("ascii", "ignore").decode())[:5])


def when(jira_time):
    return datetime.strptime(jira_time, "%Y-%m-%dT%H:%M:%S.%f%z")


def admissions(con, pol):
    site, out = pol["jira"]["site"], []
    linked = {r[0] for r in con.execute("SELECT l.ref FROM links l JOIN sessions s USING (session_id) "
                                        "WHERE l.kind = 'ticket' AND s.state != 'ended'")}
    for p in pol["projects"].values():
        if "admit" not in p:
            continue  # another Source admits the tickets of this project
        for i in search(pol, p["admit"], "summary,statuscategorychangedate"):
            if i["key"] in linked:
                continue
            f = i["fields"]
            out.append({"id": f"jira:admit:{i['key']}:{f['statuscategorychangedate']}", "kind": "ticket",
                        "ref": i["key"], "payload": {
                            "what": "ready in the sprint", "actor": "jira", "at": f["statuscategorychangedate"],
                            "url": f"{site}/browse/{i['key']}", "self": False, "admitted": True,
                            "slug": slug(f.get("summary") or "")}})
    return out


def updates(con, pol, last, start):
    keys = {r[0] for r in con.execute("SELECT l.ref FROM links l JOIN sessions s USING (session_id) "
                                             "WHERE l.kind = 'ticket' AND s.state != 'ended'")}
    if not keys:
        return []
    since = registry.parse(last) - OVERLAP
    minutes = math.ceil((start - since).total_seconds() / 60)
    # Relative JQL avoids the Jira time zone of the token owner. The search names projects, not the linked keys:
    # one deleted or bogus key in `key in (...)` can fail the whole search and stop admission too.
    # ponytail: one page of 100 issues per poll. A long daemon outage in busy projects can pass that.
    jql = f"project in ({', '.join(pol['projects'])}) AND updated >= -{minutes}m ORDER BY updated DESC"
    site, mine, out = pol["jira"]["site"], me(pol), []
    for i in search(pol, jql, "comment", expand="changelog"):
        if i["key"] not in keys:
            continue
        key, browse = i["key"], f"{site}/browse/{i['key']}"
        comment = i["fields"].get("comment") or {}
        comments = comment.get("comments") or []
        if comment.get("total", 0) > len(comments):
            # The embedded list is oldest first, so a cut drops the newest. Read the newest page instead.
            comments = get(pol, f"/issue/{key}/comment", orderBy="-created", maxResults=100).get("comments", [])
        for c in comments:
            if when(c["created"]) >= since:
                a = c.get("author") or {}
                out.append({"id": f"jira:comment:{c['id']}", "kind": "ticket", "ref": key, "payload": {
                    "what": "comment", "actor": a.get("displayName", "unknown"), "at": c["created"],
                    "url": f"{browse}?focusedCommentId={c['id']}", "self": a.get("accountId") == mine,
                    "admitted": False}})
        # ponytail: the embedded changelog holds the newest 40 histories (one ticket had 40 of 45).
        # A cut drops only old ones. More than 40 changes on one ticket in one poll would lose some.
        for h in ((i.get("changelog") or {}).get("histories") or []):
            if when(h["created"]) >= since:
                a = h.get("author") or {}
                fields = ", ".join(dict.fromkeys(x.get("field", "?") for x in h.get("items", [])))
                out.append({"id": f"jira:changelog:{h['id']}", "kind": "ticket", "ref": key, "payload": {
                    "what": f"changed {fields}", "actor": a.get("displayName", "unknown"), "at": h["created"],
                    "url": browse, "self": a.get("accountId") == mine, "admitted": False}})
    return out


def poll(con, pol, dry_run=False):
    """Insert Events for admissions and updates. Return the count. dry_run prints and writes nothing."""
    if not dry_run and not registry.due(con, "jira:next"):
        return 0
    start = datetime.now(timezone.utc)
    if not dry_run:
        registry.wait(con, "jira:next", timedelta(seconds=60))  # a failed request also waits
    last = cursor(con, "jira")
    evs = admissions(con, pol) + (updates(con, pol, last, start) if last else [])
    n = 0
    for ev in evs:
        if dry_run:
            print(json.dumps(ev))
            n += 1
        else:
            n += con.execute("INSERT OR IGNORE INTO events (id, source, kind, ref, payload) VALUES (?, 'jira', ?, ?, ?)",
                             (ev["id"], ev["kind"], ev["ref"], json.dumps(ev["payload"]))).rowcount
    if not dry_run:
        set_cursor(con, "jira", iso(start))  # first run: no update Events, start from now
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the Events, write nothing")
    ap.add_argument("--since", help="ISO time like 2026-10-28T00:00:00Z, stands in for the cursor (dry run only)")
    a = ap.parse_args()
    if a.since and not a.dry_run:
        ap.error("--since needs --dry-run")
    con, pol = registry.connect(), config.policy()
    if a.since:
        print(len([print(json.dumps(e)) for e in updates(con, pol, a.since, datetime.now(timezone.utc))]), "update events")
    print(poll(con, pol, dry_run=a.dry_run), "events")
