#!/usr/bin/env python3
"""GitHub Source: activities on policy repos' pull requests become Events, one per activity. Read only. See DESIGN.md."""
import argparse
import json
import re
import subprocess
from datetime import timedelta

import config
import harness
import registry
from registry import cursor, iso, now, set_cursor  # noqa: F401  (the Source tests use them)

OVERLAP = timedelta(minutes=10)  # GitHub can list a notification after a newer one. Ids deduplicate.
PER_PAGE = 50  # the GitHub maximum for notifications
MAX_PAGES = 10  # ponytail: 500 updates in one poll interval is the ceiling. Past it the oldest are lost.
PER_ACTIVITY_PAGE = 100


def gh(path, headers=False):
    """GET through gh. With headers=True, return (headers dict, body)."""
    out = subprocess.run([config.GH, "api", *(["-i"] if headers else []), path],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    if not headers:
        return json.loads(out)
    head, _, body = out.replace("\r\n", "\n").partition("\n\n")
    hdrs = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in head.splitlines()[1:])}
    return hdrs, json.loads(body)


def gh_all(path):
    """GET every page through gh. The activity lists come oldest first, so the newest items sit on the last page."""
    out = subprocess.run([config.GH, "api", "--paginate", "--slurp", path],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    return [item for page in json.loads(out) for item in page]


_me = None


def me():
    global _me
    if _me is None:
        _me = gh("user")["login"]
    return _me


def pr_ref(subject_url):
    """https://api.github.com/repos/o/r/pulls/5 -> https://github.com/o/r/pull/5, the string pr_link.py stores."""
    m = re.fullmatch(r"https://api\.github\.com/repos/([\w.-]+/[\w.-]+)/pulls/(\d+)", subject_url or "")
    return f"https://github.com/{m[1]}/pull/{m[2]}" if m else None


def activities(note, since, fetch=gh_all, login=me):
    """One Event per activity on the PR of a notification, from `since` up to the notification's updated_at.
    An edit or a CI run changes updated_at but adds no activity, so it makes no Event."""
    # ponytail: CI failures do not reach a session yet. Add them when a session needs them after its own run ends.
    m = re.fullmatch(r"https://api\.github\.com/repos/([\w.-]+/[\w.-]+)/pulls/(\d+)", note["subject"].get("url") or "")
    if not m:
        return []
    base, ref, until = f"repos/{m[1]}", pr_ref(note["subject"]["url"]), note["updated_at"]
    page = f"per_page={PER_ACTIVITY_PAGE}"

    def new(t):
        return bool(t) and since <= t <= until

    def ev(kind, id_, what, user, at, url):
        actor = (user or {}).get("login") or "unknown"
        return {"id": f"gh:{kind}:{id_}", "source": "github", "kind": "pr", "ref": ref,
                "payload": {"what": what, "actor": actor, "at": at, "url": url or ref,
                            "self": actor == login(), "admitted": False}}

    out = []
    for c in fetch(f"{base}/issues/{m[2]}/comments?since={since}&{page}"):
        if new(c.get("created_at")):  # an edit is not new
            out.append(ev("comment", c["id"], "comment", c.get("user"), c["created_at"], c.get("html_url")))
    reviews = [r for r in fetch(f"{base}/pulls/{m[2]}/reviews?{page}") if new(r.get("submitted_at"))]
    for r in reviews:
        out.append(ev("review", r["id"], f"review {r.get('state', '').lower().replace('_', ' ')}",
                      r.get("user"), r["submitted_at"], r.get("html_url")))
    covered = {r["id"] for r in reviews}
    for c in fetch(f"{base}/pulls/{m[2]}/comments?since={since}&{page}"):
        if new(c.get("created_at")) and c.get("pull_request_review_id") not in covered:  # the review covers it
            out.append(ev("review_comment", c["id"], "review comment", c.get("user"), c["created_at"], c.get("html_url")))
    for c in fetch(f"{base}/pulls/{m[2]}/commits?{page}"):
        at = ((c.get("commit") or {}).get("committer") or {}).get("date")
        if new(at):
            out.append(ev("commit", c["sha"], "push", c.get("author"), at, c.get("html_url")))
    return out


def head_branch(note, get=None):
    """The head branch of the PR of a notification: one read of the PR."""
    return (get or gh)(note["subject"]["url"].replace("https://api.github.com/", ""))["head"]["ref"]


def poll(con, pol, dry_run=False, since=None, fetch=gh_all, login=me, list_fn=None, get=None):
    """Insert Events for new PR notifications. Return the count. dry_run prints and writes nothing."""
    if not dry_run and not registry.due(con, "github:next"):
        return 0
    last = cursor(con, "github")
    if not since and not last:
        if not dry_run:
            set_cursor(con, "github", now())  # first run: start from now, never flood live sessions
        return 0
    since = since or iso(registry.parse(last) - OVERLAP)
    if not dry_run:
        registry.wait(con, "github:next", timedelta(seconds=60))  # a failed request also waits
    list_fn = list_fn or (lambda p: gh(p, headers=True))
    hdrs, notes, page = {}, [], 1
    while True:  # newest first. Read pages until a short page.
        h, batch = list_fn(f"notifications?all=true&per_page={PER_PAGE}&page={page}&since={since}")
        hdrs, notes = hdrs or h, notes + batch
        if len(batch) < PER_PAGE or page >= MAX_PAGES:
            break
        page += 1
    repos = {p["github"].lower() for p in pol["projects"].values() if "github" in p}
    n = 0
    for note in notes:
        if note["subject"]["type"] != "PullRequest" or note["repository"]["full_name"].lower() not in repos:
            continue
        evs = activities(note, since, fetch, login)
        # A harness with no recorder writes no pr Link, so the daemon links the PR by its head branch.
        # Only then is the extra read worth it: with every harness recording, the payload stays as before.
        if evs and not all(harness.HARNESSES[n]["recorder"] for n in harness.in_use(pol)):
            head = head_branch(note, get)
            for ev in evs:
                ev["payload"]["head"] = head
        for ev in evs:
            if dry_run:
                print(json.dumps(ev))
                n += 1
            else:
                n += con.execute("INSERT OR IGNORE INTO events (id, source, kind, ref, payload) VALUES (?, ?, ?, ?, ?)",
                                 (ev["id"], ev["source"], ev["kind"], ev["ref"], json.dumps(ev["payload"]))).rowcount
    if dry_run:
        return n
    if notes:  # the overlap returns older notes too, so the cursor never moves back
        set_cursor(con, "github", max([last or ""] + [x["updated_at"] for x in notes]))
    registry.wait(con, "github:next", timedelta(seconds=int(hdrs.get("x-poll-interval", "60"))))
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the Events, write nothing")
    ap.add_argument("--since", help="ISO time, overrides the cursor (dry run only)")
    a = ap.parse_args()
    if a.since and not a.dry_run:
        ap.error("--since needs --dry-run")
    print(poll(registry.connect(), config.policy(), dry_run=a.dry_run, since=a.since), "events")
