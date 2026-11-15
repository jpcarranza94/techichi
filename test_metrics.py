"""Check for the metric cards. No herdr, no network. Run: python3 test_metrics.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import config  # noqa: E402
import json
from datetime import datetime, timedelta, timezone

import metrics
import registry

pol = config.policy()
A = "https://github.com/acme/app/pull/"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ago(hours):
    return registry.iso(NOW - timedelta(hours=hours))


def cards(con):
    out = metrics.compute(con, pol)
    json.dumps(out)  # JSON-ready
    for c in out:
        assert set(c) == {"id", "title", "note", "kind", "unit", "data"}, c
        assert c["kind"] in ("stat", "bar", "line", "heat"), c
    return {c["id"]: c for c in out}


# The times: Z and the Jira offset. A bad one is None.
assert metrics.when("2026-01-01T10:00:00Z") == datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
assert metrics.when("2026-01-01T10:00:00.000-0500") == datetime(2026, 1, 1, 15, tzinfo=timezone.utc)
assert metrics.when("soon") is None and metrics.when(None) is None
assert (metrics.span(45 * 60), metrics.span(3 * 3600 + 300), metrics.span(52 * 3600)) == ("45m", "3h 5m", "2d 4h")

# An empty registry: every card shows, with no data and the note that says when it starts.
con = registry.connect(str(testenv.HOME / "empty.db"))
c = cards(con)
assert list(c) == [x[0] for x in metrics.CARDS]
assert c["answer"]["data"] == [] and c["answer"]["note"] == metrics.LOG
assert c["merged_per_week"]["note"] == metrics.SNAP and c["activity"]["data"] == []
assert c["now"]["data"][0] == {"label": "Sessions", "value": 0}

con = registry.connect(str(testenv.HOME / "full.db"))
q = con.execute
q("INSERT INTO sessions (session_id, role, state, agent_status, created_at, ended_at) VALUES "
  "('w1', 'worker', 'ended', 'idle', ?, ?), ('w2', 'worker', 'live', 'idle', ?, NULL)", (ago(50), ago(20), ago(10)))
for kind, ref, sid, by in [("ticket", "APP-1", "w1", "spawner"), ("pr", A + "1", "w1", "hook:pr_link"),
                           ("ticket", "APP-2", "w2", "spawner")]:
    registry.link(con, kind, ref, sid, by)
q("INSERT INTO needs VALUES ('w2', 'pick a name', ?)", (ago(5),))
# w1: needs you 2h, then agent 1h, then ended. w2: needs you since 5h, still open.
q("INSERT INTO status_log VALUES ('w1', 'YOU', 'needs you', ?), ('w1', 'AGENT', 'working', ?), "
  "('w1', 'ENDED', 'ended', ?), ('w2', 'YOU', 'needs you', ?), ('w2', 'CHECK', 'bad', 'never')",
  (ago(30), ago(28), ago(27), ago(5)))
registry.set_ref(con, "pr", A + "1", {"number": 1, "state": "MERGED", "updatedAt": ago(20)})
registry.set_ref(con, "pr", A + "9", {"number": 9, "state": "MERGED", "updatedAt": ago(20)})  # no pr Link: not yours


def ev(id_, what, actor, at, self=False, ref=A + "1"):
    q("INSERT INTO events (id, source, kind, ref, payload) VALUES (?, 'github', 'pr', ?, ?)",
      (id_, ref, json.dumps({"what": what, "actor": actor, "at": at, "self": self})))


ev("p1", "push", "alice", ago(40), self=True)
ev("r1", "review changes requested", "bob", ago(36))
ev("p2", "push", "alice", ago(35), self=True)
ev("r2", "review changes requested", "bob", ago(33))
ev("r3", "review commented", "alice", ago(32.5), self=True)  # the author replies: no review
ev("r4", "review approved", "coderabbitai", ago(32))  # a bot
ev("r5", "review approved", "bob", ago(31))
ev("j1", "comment", "Bob", "2026-01-01T10:00:00.000-0500", ref="APP-1")  # Jira time, outside the window
q("INSERT INTO events (id, source, kind, ref, payload) VALUES ('bad', 'github', 'pr', 'x', 'not json')")
c = cards(con)

assert c["now"]["data"][1] == {"label": "Wait for you", "value": 1}, c["now"]
assert c["now"]["data"][2]["value"].endswith(", you")
assert c["answer"]["data"] == [{"label": "Median", "value": "2h 0m"}, {"label": "Slowest", "value": "2h 0m"},
                               {"label": "Answers", "value": 1}]  # the open wait of w2 is no answer yet
assert len(c["answer_by_hour"]["data"]) == 1 and c["answer_by_hour"]["data"][0]["value"] == 120.0
assert sum(x["value"] for x in c["answers_heat"]["data"]) == 1
assert c["time_by_group"]["data"] == [{"label": "YOU", "value": 7.0}, {"label": "AGENT", "value": 1.0}]
# bob: first review 4h after the first push. The author and the bot do not count.
assert c["review_wait"]["data"] == [{"label": "bob", "value": 4.0}], c["review_wait"]
assert c["review_rounds"]["data"] == [{"label": "2", "value": 1}]
assert c["start_to_merge"]["data"] == [{"label": "APP-1 #1", "value": 30.0}], c["start_to_merge"]
assert sum(x["value"] for x in c["merged_per_week"]["data"]) == 1
assert sum(x["value"] for x in c["activity"]["data"]) == 7  # the Jira one is old, the bad row is skipped
assert c["slots"]["kind"] == "line" and max(x["value"] for x in c["slots"]["data"]) == 1

# One card that fails keeps the others.
metrics.CARDS[1] = metrics.CARDS[1][:6] + (lambda d: 1 / 0,)
c = cards(con)
assert c["answer"]["note"] == "error: ZeroDivisionError" and c["answer"]["data"] == [] and c["activity"]["data"]
print("ok")
