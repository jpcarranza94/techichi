"""Check for the GitHub Source with fixture notifications and activities. No network. Run: python3 test_github.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import config  # noqa: E402
import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import github_source as g
import registry

pol = config.policy()
db = testenv.HOME / "registry.db"  # the hook below writes there
con = registry.connect(db)
ME, CR = "op-login", "coderabbitai[bot]"
login = lambda: ME


def note(nid, repo, num, at, typ="PullRequest"):
    kind = "pulls" if typ == "PullRequest" else "issues"
    url = f"https://api.github.com/repos/{repo}/{kind}/{num}"
    return {"id": nid, "reason": "author", "updated_at": at, "repository": {"full_name": repo},
            "subject": {"type": typ, "url": url, "latest_comment_url": url}}


def user(login_):
    return {"login": login_}


H = "https://github.com/acme/app/pull/577"
# A PR timeline: a review bot summary, a bot review, own replies, and two pushes.
ACT = {
    "issues/577/comments": [  # the CodeRabbit summary. Its edits change updated_at, never created_at.
        {"id": 7000000001, "user": user(CR), "created_at": "2026-10-28T15:29:43Z", "updated_at": "2026-10-28T15:44:49Z",
         "html_url": f"{H}#issuecomment-7000000001"}],
    "pulls/577/reviews": [
        {"id": 5000000001, "user": user(CR), "state": "COMMENTED", "submitted_at": "2026-10-28T15:38:29Z",
         "html_url": f"{H}#pullrequestreview-5000000001"},
        *[{"id": i, "user": user(ME), "state": "COMMENTED", "submitted_at": t, "html_url": f"{H}#pullrequestreview-{i}"}
          for i, t in ((5000000002, "2026-10-28T15:42:17Z"), (5000000003, "2026-10-28T15:42:21Z"),
                       (5000000004, "2026-10-28T15:42:24Z"))]],
    "pulls/577/comments": [
        *[{"id": i, "user": user(CR), "created_at": "2026-10-28T15:38:26Z", "pull_request_review_id": 5000000001,
           "html_url": f"{H}#discussion_r{i}"} for i in (4000000001, 4000000002, 4000000003)],
        *[{"id": i, "user": user(ME), "created_at": t, "pull_request_review_id": r, "html_url": f"{H}#discussion_r{i}"}
          for i, t, r in ((4000000004, "2026-10-28T15:42:17Z", 5000000002), (4000000005, "2026-10-28T15:42:20Z", 5000000003),
                          (4000000006, "2026-10-28T15:42:24Z", 5000000004))]],
    "pulls/577/commits": [
        {"sha": "a1b2c3d", "author": user(ME), "commit": {"committer": {"date": "2026-10-28T15:29:22Z"}},
         "html_url": "https://github.com/acme/app/commit/a1b2c3d"},
        {"sha": "d4e5f60", "author": user(ME), "commit": {"committer": {"date": "2026-10-28T15:42:05Z"}},
         "html_url": "https://github.com/acme/app/commit/d4e5f60"}],
}
fetched = []


def fetch(path):
    fetched.append(path)
    m = re.fullmatch(r"repos/acme/app/((?:issues|pulls)/577/\w+)\?.*", path)
    return ACT[m[1]] if m else []


# The seven notification updates of PR 577 between 15:30 and 15:45. One thread, so one row per poll.
UPDATES = ["2026-10-28T15:30:05Z", "2026-10-28T15:32:09Z", "2026-10-28T15:38:36Z", "2026-10-28T15:42:23Z",
           "2026-10-28T15:42:45Z", "2026-10-28T15:43:28Z", "2026-10-28T15:44:49Z"]


def poll(notes, **kw):
    con.execute("DELETE FROM cursors WHERE source = 'github:next'")
    return g.poll(con, pol, fetch=fetch, login=login, list_fn=lambda path: ({"x-poll-interval": "60"}, notes), **kw)


# First run: cursor set to now, nothing inserted, GitHub not called.
assert g.poll(con, pol, fetch=fetch, login=login, list_fn=lambda p: 1 / 0) == 0 and g.cursor(con, "github")

# Each poll sees the notification at its next update. Activities count once, edits and CI count never.
con.execute("UPDATE cursors SET value = '2026-10-28T15:28:00Z' WHERE source = 'github'")
for at in UPDATES:
    poll([note("10000000001", "acme/app", 577, at)])
ev = {r["id"]: json.loads(r["payload"]) for r in con.execute("SELECT * FROM events")}
assert sorted(ev) == sorted(["gh:comment:7000000001", "gh:review:5000000001", "gh:review:5000000002",
                             "gh:review:5000000003", "gh:review:5000000004", "gh:commit:a1b2c3d", "gh:commit:d4e5f60"]), sorted(ev)
not_self = {k for k, p in ev.items() if not p["self"]}
assert not_self == {"gh:comment:7000000001", "gh:review:5000000001"}  # the two items the session needs
assert ev["gh:review:5000000001"] == {"what": "review commented", "actor": CR, "at": "2026-10-28T15:38:29Z",
                                      "url": f"{H}#pullrequestreview-5000000001", "self": False, "admitted": False}
assert ev["gh:comment:7000000001"]["url"] == f"{H}#issuecomment-7000000001"
assert ev["gh:commit:d4e5f60"]["what"] == "push" and ev["gh:commit:d4e5f60"]["self"] is True
assert not any(k.startswith("gh:review_comment:") for k in ev)  # the reviews cover their comments
assert {r["ref"] for r in con.execute("SELECT ref FROM events")} == {H}

# A review comment whose review is outside the window still counts by itself.
only = g.activities(note("x", "acme/app", 577, "2026-10-28T15:42:30Z"), "2026-10-28T15:39:00Z",
                    lambda p: [] if "reviews" in p else fetch(p), login)
assert {e["id"] for e in only} == {"gh:review_comment:4000000004", "gh:review_comment:4000000005",
                                   "gh:review_comment:4000000006", "gh:commit:d4e5f60"}

# The dry run prints and writes nothing, not even the cursors.
n = con.execute("SELECT count(*) FROM events").fetchone()[0]
cur = g.cursor(con, "github")
con.execute("DELETE FROM cursors WHERE source = 'github:next'")
with contextlib.redirect_stdout(io.StringIO()):
    assert g.poll(con, pol, dry_run=True, since="2026-10-28T15:25:00Z", fetch=fetch, login=login,
                  list_fn=lambda p: ({}, [note("1", "acme/app", 577, "2026-10-28T15:44:49Z")])) == 7
assert con.execute("SELECT count(*) FROM events").fetchone()[0] == n and g.cursor(con, "github") == cur
assert g.cursor(con, "github:next") is None

# Filters: repos from the policy (case does not matter), PullRequest subjects only.
fetched.clear()
poll([note("3", "ACMe/APP", 577, "2026-10-28T15:44:50Z"), note("5", "acme/app", 10, "2026-10-28T15:44:50Z", "Issue"),
      note("6", "acme/other", 118, "2026-10-28T15:44:50Z"), note("7", "other/repo", 2176, "2026-10-28T15:44:50Z")])
assert fetched and all("/577/" in p for p in fetched)

# The overlap: requests ask from 10 minutes before the cursor, and the cursor never moves back.
calls = []
con.execute("UPDATE cursors SET value = '2026-10-28T15:44:50Z' WHERE source = 'github'")
con.execute("DELETE FROM cursors WHERE source = 'github:next'")
g.poll(con, pol, fetch=fetch, login=login,
       list_fn=lambda p: (calls.append(p), ({}, [note("1", "acme/app", 577, "2026-10-28T15:40:00Z")]))[1])
assert "all=true" in calls[0] and "since=2026-10-28T15:34:50Z" in calls[0] and g.cursor(con, "github") == "2026-10-28T15:44:50Z"

# X-Poll-Interval and backoff: the next call waits, and a failed request also waits.
assert g.cursor(con, "github:next") > g.now()
assert g.poll(con, pol, fetch=fetch, login=login, list_fn=lambda p: 1 / 0) == 0
con.execute("DELETE FROM cursors WHERE source = 'github:next'")
try:
    g.poll(con, pol, fetch=fetch, login=login, list_fn=lambda p: 1 / 0)
except ZeroDivisionError:
    pass
assert g.cursor(con, "github:next") > g.now()

# The ref equals the string that hooks/pr_link.py stores for the same PR.
env = dict(os.environ, HERDR_SESSION="default", HERDR_PANE_ID="wX:p1")
hook = {"session_id": "s1", "tool_input": {"command": "gh pr create --fill"}, "tool_response": {"stdout": H + "\n"}}
subprocess.run(["python3", Path(__file__).parent / "hooks/pr_link.py"], input=json.dumps(hook), text=True, env=env, timeout=5)
assert con.execute("SELECT ref FROM links WHERE kind = 'pr'").fetchone()[0] == H

# A full notification page reads the next page.
pages = []
full = [note(str(i), "acme/app", i, "2026-10-28T15:50:00Z") for i in range(g.PER_PAGE)]
con.execute("DELETE FROM cursors WHERE source = 'github:next'")
g.poll(con, pol, fetch=lambda p: [], login=login,
       list_fn=lambda path: (pages.append(path), ({}, full if "&page=1&" in path else []))[1])
assert [re.search(r"&page=(\d+)", x)[1] for x in pages] == ["1", "2"]
# All pages: the lists come oldest first. A new review on page 2 must still make an Event.
d = Path(tempfile.mkdtemp())
old = [{"id": i, "user": user(ME), "state": "COMMENTED", "submitted_at": "2026-10-28T12:00:00Z", "html_url": f"{H}#r{i}"}
       for i in range(g.PER_ACTIVITY_PAGE)]
new = {"id": 999, "user": user(CR), "state": "COMMENTED", "submitted_at": "2026-10-28T16:00:00Z", "html_url": f"{H}#r999"}
(d / "reviews.json").write_text(json.dumps([old, [new]]))
stub = d / "gh"
stub.write_text(f"""#!/bin/sh
[ "$2 $3" = "--paginate --slurp" ] || exit 9
case "$4" in *reviews*) cat {d}/reviews.json ;; *) echo '[[]]' ;; esac
""")
stub.chmod(0o755)
real_gh, config.GH = config.GH, str(stub)
try:
    assert len(g.gh_all("repos/acme/app/pulls/577/reviews?per_page=100")) == g.PER_ACTIVITY_PAGE + 1
    got = g.activities(note("n", "acme/app", 577, "2026-10-28T16:00:05Z"), "2026-10-28T15:50:00Z", login=login)
finally:
    config.GH = real_gh
assert [e["id"] for e in got] == ["gh:review:999"] and got[0]["payload"]["actor"] == CR
print("ok")
