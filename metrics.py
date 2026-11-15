"""Metrics for the board page: small cards about the patterns of work. See DESIGN.md section "Metrics".

Pure, like status.py: SQL reads only. No writes, no herdr, no network. compute() returns a JSON-ready list of cards.
"""
import json
import statistics
from datetime import datetime, timedelta, timezone
from functools import cached_property

import registry
import status

LOOKBACK = timedelta(days=14)  # Events before the window: the push or the request that starts a review wait
LOG = "No data yet. It starts when the board logs statuses."
SNAP = "No data yet. It starts when the GitHub Source stores PR snapshots."
GH = "No data yet. It starts when the GitHub Source logs reviews."
MERGE = ""  # the snapshot holds mergedAt. An old snapshot without it falls back to updatedAt.


def when(t):
    """An aware datetime from ISO text, with Z or a Jira offset such as -0500. None for a bad value."""
    try:
        d = datetime.fromisoformat(t)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def span(sec):
    """45m, 3h 5m, or 2d 4h."""
    m = int(sec // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h {m % 60}m" if m < 1440 else f"{m // 1440}d {m % 1440 // 60}h"


def heat(times):
    cells = {}
    for t in times:
        t = t.astimezone()  # the system local zone
        cells[t.weekday(), t.hour] = cells.get((t.weekday(), t.hour), 0) + 1
    return [{"day": d, "hour": h, "value": v} for (d, h), v in sorted(cells.items())]


def week(t):
    d = t.astimezone().date()
    return (d - timedelta(days=d.weekday())).isoformat()


def review(e, pol):
    """A review by a person: approved, commented, or changes requested. A review comment is no review."""
    return e["what"].startswith("review ") and e["what"] not in ("review comment", "review requested") \
        and not status.bot(e["actor"], pol)


class Data:
    """The rows that the cards share. Each one is read once, on first use."""

    def __init__(self, con, pol, days):
        self.con, self.pol = con, pol
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.start = self.now - timedelta(days=days)

    @cached_property
    def segs(self):
        """(grp, status, start, end) for each status_log row. A row ends at the next row of its Session.
        The last row runs until now, unless it is ENDED."""
        out, prev = [], None
        for r in self.con.execute("SELECT session_id, grp, status, at FROM status_log ORDER BY session_id, rowid"):
            at = when(r["at"])
            if at is None:
                continue
            if prev and prev[1] != "ENDED":
                out.append(prev[1:] + (at if prev[0] == r["session_id"] else self.now,))
            prev = (r["session_id"], r["grp"], r["status"], at)
        if prev and prev[1] != "ENDED":
            out.append(prev[1:] + (self.now,))
        return out

    @cached_property
    def answers(self):
        """(start, end) of each `needs you` wait that ended in the window. An open wait is no answer yet."""
        return [(s, e) for _, st, s, e in self.segs if st == "needs you" and self.start <= e < self.now]

    @cached_property
    def events(self):
        """The Events inserted since the window start minus LOOKBACK, with the payload time parsed.
        A bad row is skipped."""
        out = []
        for r in self.con.execute(
                "SELECT ref, json_extract(payload, '$.what') AS what, json_extract(payload, '$.actor') AS actor, "
                "json_extract(payload, '$.at') AS at, json_extract(payload, '$.self') AS self FROM events "
                "WHERE created_at >= ? AND json_valid(payload)", (registry.iso(self.start - LOOKBACK),)):
            at = when(r["at"])
            if at and isinstance(r["what"], str):
                out.append({"ref": r["ref"], "what": r["what"], "actor": str(r["actor"] or "unknown"), "at": at,
                            "self": bool(r["self"])})
        return out

    @cached_property
    def prs(self):
        return {r[0] for r in self.con.execute("SELECT ref FROM links WHERE kind = 'pr'")}

    @cached_property
    def merged(self):
        """(ref, number, merge time) for each PR of a pr Link whose snapshot says MERGED."""
        out = []
        for r in self.con.execute("SELECT ref, state FROM refs WHERE kind = 'pr'"):
            s = json.loads(r["state"])
            if r["ref"] in self.prs and s.get("state") == "MERGED" and (at := when(s.get("mergedAt") or s.get("updatedAt"))):
                out.append((r["ref"], s.get("number"), at))
        return out


def now_card(d):
    b = status.board(d.con, d.pol, {})
    rows = {g["name"]: g["rows"] for g in b["groups"]}
    whom = (("YOU", "you"), ("AGENT", "agent"), ("WAITS FOR OTHERS", "others"))
    oldest = min(((when(r["since"]), who) for g, who in whom for r in rows[g] if when(r["since"])), default=None)
    wait = f"{span((d.now - oldest[0]).total_seconds())}, {oldest[1]}" if oldest else "-"
    return [{"label": "Sessions", "value": sum(len(v) for v in rows.values())},
            {"label": "Wait for you", "value": len(rows["YOU"])},
            {"label": "Oldest wait", "value": wait},
            {"label": "Slots", "value": f"{b['slots']['used']} / {b['slots']['max']}"}]


def answer(d):
    w = [(e - s).total_seconds() for s, e in d.answers]
    return [{"label": "Median", "value": span(statistics.median(w))}, {"label": "Slowest", "value": span(max(w))},
            {"label": "Answers", "value": len(w)}] if w else []


def answer_by_hour(d):
    by = {}
    for s, e in d.answers:
        by.setdefault(s.astimezone().hour, []).append((e - s).total_seconds() / 60)
    return [{"label": f"{h:02d}", "value": round(statistics.median(v), 1)} for h, v in sorted(by.items())]


def time_by_group(d):
    tot = {}
    for g, _, s, e in d.segs:
        if e > d.start:
            tot[g] = tot.get(g, 0) + (e - max(s, d.start)).total_seconds() / 3600
    return [{"label": g, "value": round(tot[g], 1)} for g in status.GROUPS if g in tot]


def review_wait(d):
    first, starts, pushers = {}, {}, {}  # (ref, who): first review time. ref: the push and request times.
    for e in d.events:
        if e["what"] in ("push", "review requested"):
            starts.setdefault(e["ref"], []).append(e["at"])
        if e["what"] == "push":
            pushers.setdefault(e["ref"], set()).add(e["actor"])
    for e in d.events:  # a person who pushed to the PR is its author: a reply to a comment is no review
        if review(e, d.pol) and e["actor"] not in pushers.get(e["ref"], ()):
            key = (e["ref"], "you" if e["self"] else e["actor"])
            first[key] = min(first.get(key, e["at"]), e["at"])
    by = {}
    for (ref, who), at in first.items():
        before = [t for t in starts.get(ref, []) if t <= at]
        if at >= d.start and before:
            by.setdefault(who, []).append((at - max(before)).total_seconds() / 3600)
    out = [{"label": w, "value": round(statistics.median(v), 1)} for w, v in by.items()]
    return sorted(out, key=lambda x: -x["value"])


def start_to_merge(d):
    born = {}  # PR URL: (first start of a Session with its pr Link, the ticket of that Session)
    for r in d.con.execute("SELECT l.ref, s.created_at, (SELECT group_concat(t.ref) FROM links t "
                           "WHERE t.session_id = s.session_id AND t.kind = 'ticket') AS tickets "
                           "FROM links l JOIN sessions s USING (session_id) WHERE l.kind = 'pr'"):
        t = when(r["created_at"])
        key = next((k for k in (r["tickets"] or "").split(",") if k.split("-")[0] in d.pol["projects"]), "")
        if t and (r["ref"] not in born or t < born[r["ref"]][0]):
            born[r["ref"]] = (t, key)
    rows = sorted((at, f"{born[ref][1]} #{n}".strip(), (at - born[ref][0]).total_seconds() / 3600)
                  for ref, n, at in d.merged if at >= d.start and ref in born)
    return [{"label": lab, "value": round(h, 1)} for _, lab, h in rows]


def merged_per_week(d):
    n = {}
    for _, _, at in d.merged:
        if at >= d.start:
            n[week(at)] = n.get(week(at), 0) + 1
    return [{"label": w, "value": v} for w, v in sorted(n.items())]


def review_rounds(d):
    n = {}
    for e in d.events:
        if e["ref"] in d.prs and e["at"] >= d.start and not e["self"] and review(e, d.pol):
            n[e["ref"]] = n.get(e["ref"], 0) + (e["what"] == "review changes requested")
    hist = {}
    for v in n.values():
        hist[min(v, 3)] = hist.get(min(v, 3), 0) + 1
    return [{"label": "3+" if k == 3 else str(k), "value": hist[k]} for k in sorted(hist)]


def activity(d):
    return heat(e["at"] for e in d.events if e["at"] >= d.start)


def answers_heat(d):
    return heat(e for _, e in d.answers)


def slots(d):
    spans = []
    for r in d.con.execute("SELECT DISTINCT s.session_id, s.state, s.created_at, s.ended_at, s.last_seen "
                           "FROM sessions s JOIN links l USING (session_id) "
                           "WHERE l.kind IN ('ticket','review') AND l.created_by = 'spawner'"):
        a = when(r["created_at"])
        b = d.now if r["state"] in ("registered", "live") else when(r["ended_at"] or r["last_seen"])
        if a and b and b > d.start:
            spans.append((a, b))
    if not spans:
        return []
    peak, t = {}, d.start.replace(minute=0, second=0)
    while t <= d.now:  # ponytail: one sample an hour. A Session shorter than an hour can be missed.
        day = t.astimezone().date().isoformat()
        peak[day] = max(peak.get(day, 0), sum(a <= t < b for a, b in spans))
        t += timedelta(hours=1)
    return [{"label": k, "value": v} for k, v in sorted(peak.items())]


# id, kind, title, unit, note, the note with no data (None: keep the note), the data.
CARDS = [
    ("now", "stat", "Now", "", "The board now: Sessions on it, the ones that wait for you, the oldest wait and for "
     "whom, and the slots in use.", None, now_card),
    ("answer", "stat", "Your answer time", "", "How long a Session stays in `needs you` before it leaves it, from the "
     "status log, for the waits that ended in the window.", LOG, answer),
    ("answer_by_hour", "bar", "Answer time by hour", "min", "Median minutes in `needs you`, by the local hour when the "
     "wait started, from the status log.", LOG, answer_by_hour),
    ("time_by_group", "bar", "Where Session time goes", "h", "Hours that Sessions spent in each board group in the "
     "window, from the status log.", LOG, time_by_group),
    ("review_wait", "bar", "Review wait by reviewer", "h", "Median hours from the last push or review request on a PR "
     "to the first review by that person, from the GitHub Events. Bots and people who pushed to the PR do not "
     "count.", GH, review_wait),
    ("start_to_merge", "bar", "Start to merge", "h", "Hours from the start of the first Session with the PR to its "
     "merge, for each of your PRs merged in the window." + MERGE, SNAP, start_to_merge),
    ("merged_per_week", "bar", "PRs merged per week", "PRs", "Your PRs merged each week, by the local Monday of the "
     "week." + MERGE, SNAP, merged_per_week),
    ("review_rounds", "bar", "Review rounds per PR", "PRs", "Your PRs by the count of `changes requested` reviews from "
     "people, for the PRs that got a review in the window, from the GitHub Events.", GH, review_rounds),
    ("activity", "heat", "Activity", "Events", "Events from GitHub, Jira, and Slack by local weekday and hour of the "
     "update.", "No data yet. It starts when a Source logs Events.", activity),
    ("answers_heat", "heat", "When you answer", "answers", "Each time a Session leaves `needs you`, by local weekday "
     "and hour, from the status log.", LOG, answers_heat),
    ("slots", "line", "Slots in use", "slots", "The most spawned workers and reviewers that ran at the same time on "
     "each local day, from the Session start and end times.", "No data yet. It starts when the factory spawns.", slots),
]


def compute(con, pol, days=30):
    """The cards in display order. A card that fails has no data and the note "error: <type>"."""
    d = Data(con, pol, days)
    out = []
    for id_, kind, title, unit, note, empty, fn in CARDS:
        c = {"id": id_, "title": title, "note": note, "kind": kind, "unit": unit, "data": []}
        try:
            c["data"] = fn(d)
            if not c["data"] and empty:
                c["note"] = empty
        except Exception as e:  # one bad card must not lose the others
            c["note"] = f"error: {type(e).__name__}"
        out.append(c)
    return out
