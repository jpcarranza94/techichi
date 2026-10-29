"""Check for the Jira Source with a stub urlopen. No network, no real token. Run: python3 test_jira.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import base64
import contextlib
import io
import json
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path

import jira_source as j
import registry

d = Path(tempfile.mkdtemp())
TOKEN = "tok-SECRET-123"
(d / "token").write_text(TOKEN + "\n")
pol = registry.policy()
pol["jira"] = {**pol["jira"], "token_file": str(d / "token")}
con = registry.connect(d / "r.db")
API, SITE = pol["jira"]["api"], pol["jira"]["site"]

ADMIT = {"APP": [{"key": "APP-10", "fields": {"summary": "Add CSV export to the admin report page!",
                                             "statuscategorychangedate": "2026-10-28T09:00:00.000-0400"}},
                 {"key": "APP-11", "fields": {"summary": "Linked one", "statuscategorychangedate": "2026-10-28T09:00:00.000-0400"}}],
         "WEB": []}
UPDATES = [{"key": "APP-11", "fields": {"comment": {"comments": [
    {"id": "501", "created": "2026-10-28T10:00:00.000-0400", "author": {"displayName": "Alice", "accountId": "a-em"}},
    {"id": "500", "created": "2026-10-27T08:00:00.000-0400", "author": {"displayName": "Alice", "accountId": "a-em"}},
    {"id": "502", "created": "2026-10-28T10:05:00.000-0400", "author": {"displayName": "operator", "accountId": "a-me"}}]}},
    "changelog": {"histories": [
        {"id": "900", "created": "2026-10-28T10:01:00.000-0400", "author": {"displayName": "Bob", "accountId": "a-sh"},
         "items": [{"field": "status"}, {"field": "assignee"}, {"field": "status"}]},
        {"id": "899", "created": "2026-10-26T10:01:00.000-0400", "author": {"displayName": "Bob"}, "items": []}]}},
    # Updated, but no Session links it.
    {"key": "APP-99", "fields": {"comment": {"comments": [
        {"id": "700", "created": "2026-10-28T10:00:00.000-0400", "author": {"displayName": "X"}}]}}},
    # Linked, and the embedded comment list is cut: total 3, one returned.
    {"key": "WEB-5", "fields": {"comment": {"total": 3, "comments": [
        {"id": "800", "created": "2026-10-20T10:00:00.000-0400", "author": {"displayName": "Old"}}]}}}]
NEWEST = {"comments": [{"id": "802", "created": "2026-10-28T10:10:00.000-0400", "author": {"displayName": "New"}},
                       {"id": "801", "created": "2026-10-21T10:00:00.000-0400", "author": {"displayName": "Old"}}]}
seen, fail = [], {"code": None}


def fake_urlopen(req, timeout=None):
    auth = req.headers["Authorization"]
    assert base64.b64decode(auth.split()[1]).decode() == f"{pol['jira']['email']}:{TOKEN}"
    assert req.get_method() == "GET" and req.full_url.startswith(API)
    if fail["code"]:
        raise urllib.error.HTTPError(req.full_url, fail["code"], f"bad {auth}", {}, io.BytesIO(TOKEN.encode()))
    path, _, q = req.full_url[len(API):].partition("?")
    q = dict(urllib.parse.parse_qsl(q))
    seen.append((path, q))
    if path == "/myself":
        body = {"accountId": "a-me"}
    elif path == "/issue/WEB-5/comment":
        assert q["orderBy"] == "-created"
        body = NEWEST
    elif q["jql"].startswith("project = APP"):
        body = {"issues": ADMIT["APP"]}
    elif q["jql"].startswith("project = WEB"):
        body = {"issues": ADMIT["WEB"]}
    else:
        body = {"issues": UPDATES}
    return io.BytesIO(json.dumps(body).encode())


j.urllib.request.urlopen = fake_urlopen

con.execute("INSERT INTO sessions (session_id, role, state) VALUES ('s1', 'worker', 'live')")
registry.link(con, "ticket", "APP-11", "s1", "spawner")
registry.link(con, "ticket", "WEB-5", "s1", "hook:ticket_link")
registry.link(con, "ticket", "UTF-8", "s1", "hook:ticket_link")  # a bogus key from Skill arguments

# First run: admissions only, the cursor is the poll start, no update search.
assert j.poll(con, pol) == 1
assert not any(q.get("jql", "").startswith("project in") for _, q in seen)
rows = {r["id"]: (r["kind"], r["ref"], json.loads(r["payload"])) for r in con.execute("SELECT * FROM events")}
assert list(rows) == ["jira:admit:APP-10:2026-10-28T09:00:00.000-0400"]  # APP-11 has a live ticket Link
kind, ref, p = rows["jira:admit:APP-10:2026-10-28T09:00:00.000-0400"]
assert (kind, ref) == ("ticket", "APP-10")
assert p == {"what": "ready in the sprint", "actor": "jira", "at": "2026-10-28T09:00:00.000-0400",
             "url": f"{SITE}/browse/APP-10", "self": False, "admitted": True, "slug": "add-csv-export-to-the"}
assert j.cursor(con, "jira") and j.cursor(con, "jira:next") > j.now()

# Backoff: the next poll inside the interval makes no request.
n = len(seen)
assert j.poll(con, pol) == 0 and len(seen) == n

# Updates: cursor 13:55Z, so items from 13:45Z (09:45 EDT) onward are new.
con.execute("UPDATE cursors SET value = '2026-10-28T13:55:00Z' WHERE source = 'jira'")
con.execute("DELETE FROM cursors WHERE source = 'jira:next'")
assert j.poll(con, pol) == 4
q = [q for _, q in seen if q.get("jql", "").startswith("project in")][-1]
# Projects from the policy, never the linked keys, so the bogus key UTF-8 cannot fail the search.
assert q["jql"].startswith("project in (APP, WEB) AND updated >= -") and "UTF-8" not in q["jql"]
assert q["jql"].endswith("m ORDER BY updated DESC") and q["expand"] == "changelog"
mins = int(q["jql"].rsplit("-", 1)[1].split("m")[0])
assert mins >= 11  # minutes since the cursor plus the 10 minute overlap
ev = {r["id"]: json.loads(r["payload"]) for r in con.execute("SELECT * FROM events WHERE source = 'jira'")}
assert ev["jira:comment:501"] == {"what": "comment", "actor": "Alice", "at": "2026-10-28T10:00:00.000-0400",
                                  "url": f"{SITE}/browse/APP-11?focusedCommentId=501", "self": False, "admitted": False}
assert ev["jira:comment:502"]["self"] is True
assert ev["jira:changelog:900"]["what"] == "changed status, assignee" and ev["jira:changelog:900"]["url"] == f"{SITE}/browse/APP-11"
assert "jira:comment:500" not in ev and "jira:changelog:899" not in ev  # older than cursor minus the overlap
assert "jira:comment:700" not in ev  # APP-99 has no Link
assert "jira:comment:802" in ev and "jira:comment:800" not in ev  # the newest page replaces the cut list

# The dry run writes nothing, not even a cursor.
before = list(con.execute("SELECT * FROM cursors")), con.execute("SELECT count(*) FROM events").fetchone()[0]
con.execute("UPDATE cursors SET value = '2026-10-28T13:55:00Z' WHERE source = 'jira'")
before = list(map(tuple, con.execute("SELECT * FROM cursors"))), before[1]
with contextlib.redirect_stdout(io.StringIO()):
    assert j.poll(con, pol, dry_run=True) == 5
assert (list(map(tuple, con.execute("SELECT * FROM cursors"))), con.execute("SELECT count(*) FROM events").fetchone()[0]) == before

# No error text contains the token, also when the server echoes it.
fail["code"] = 401
con.execute("DELETE FROM cursors WHERE source = 'jira:next'")
try:
    j.poll(con, pol)
    raise AssertionError("no error")
except RuntimeError as e:
    text = f"{e!s} {e!r} {e.__cause__!r} {e.__context__!r}"
    assert TOKEN not in text and base64.b64encode(f"{pol['jira']['email']}:{TOKEN}".encode()).decode() not in text, text
    assert "HTTP 401" in str(e)
assert j.slug("Ünïcode — fix: the THING, now please!") == "ncode-fix-the-thing-now"
print("ok")
