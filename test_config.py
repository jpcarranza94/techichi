"""Check for the config: the policy defaults and checks, the herdr socket, the hook gate, the cursors, and the
skill render. Run: python3 test_config.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import contextlib
import io
import json
import os
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

import config
import herdr
import install
import registry

H = testenv.HOME
assert config.HOME == H and config.DB == H / "registry.db" and config.POLICY == H / "policy.toml"

# The smallest policy: one project and [jira]. Every other table gets its defaults.
MIN = {"projects": {"APP": {"repo": "~/app", "github": "a/app", "admit": "project = APP", "start": {"skill": "s"}}},
       "jira": {"api": "x", "site": "x", "email": "x", "token_file": "x"}}
pol = config.complete(MIN)
f = pol["factory"]
assert (f["herdr_session"], f["metadata_source"], f["inbox_ref"]) == ("default", "techichi", "operator")
assert (f["worktrees"], f["helper_cwd"], f["relay_cwd"]) == (H / "worktrees", H, H / "relay")
assert pol["skills"] == {"worker": "techichi-worker", "planner": "techichi-planner", "relay": "techichi-relay"}
assert (pol["spawn"], pol["hold"], pol["route"]["inbox"]) == ({"max_active": 0, "gap_seconds": 30}, {"notify_after_minutes": 60}, ["planner"])
assert pol["projects"]["APP"]["repo"] == str(Path.home() / "app") and pol["projects"]["APP"]["setup"] == []
assert "slack" not in pol  # no [slack]: no relay
# The policy values win over the defaults, key by key. Paths expand.
pol = config.complete({**MIN, "factory": {"herdr_session": "work", "worktrees": "~/wt"}, "spawn": {"max_active": 4},
                       "slack": {"me": "U1", "channels": [], "hosts": []}})
assert pol["factory"]["worktrees"] == Path.home() / "wt" and pol["factory"]["inbox_ref"] == "operator"
assert pol["spawn"] == {"max_active": 4, "gap_seconds": 30} and pol["slack"]["poll_minutes"] == 5
assert config.complete(MIN) == config.complete(MIN) and "factory" not in MIN  # the input stays as it was
assert config.policy()["slack"]["hosts"] == ["acme.slack.com"]  # the example policy loads and passes


# A bad policy fails at load, with the table and the keys it needs.
def bad(p):
    try:
        config.complete(p, "p.toml")
    except config.PolicyError as e:
        return str(e)
    raise AssertionError(f"accepted {p}")


assert bad({"jira": MIN["jira"]}) == "p.toml: no [projects.<KEY>] table"
assert bad({"projects": MIN["projects"], "factory": {"sources": ["github", "jira"]}}) == \
    "p.toml: [jira] needs api, site, email, token_file"  # [jira] only when the Jira Source is on
assert config.complete({"projects": MIN["projects"]})["factory"]["sources"] == ["github"]  # no Jira, no Slack
assert bad({**MIN, "projects": {"WEB": {"repo": "r", "start": {}}}}) == "p.toml: [projects.WEB] needs github"
assert bad({**MIN, "slack": {"me": "U1"}}) == "p.toml: [slack] needs channels, hosts"
assert bad({**MIN, "projects": {"WEB": {**MIN["projects"]["APP"], "start": {}}}}) == "p.toml: [projects.WEB.start] needs skill"

# The herdr socket: the default session sits at the top of the herdr folder, a named one in sessions/.
assert herdr.socket_path("default") == Path.home() / ".config/herdr/herdr.sock"
assert herdr.socket_path("work") == Path.home() / ".config/herdr/sessions/work/herdr.sock"
assert herdr.session() == "default" and herdr.socket_path() == herdr.socket_path("default")

# Timer cursors: a missing one is due. A "next" time is due when it comes. A "last" time is due after its interval.
con = registry.connect(Path(tempfile.mkdtemp(), "c.db"))
assert registry.due(con, "x:next")
registry.wait(con, "x:next", timedelta(minutes=5))
assert not registry.due(con, "x:next") and registry.due(con, "x:next", at=registry.now(timedelta(minutes=6)))
registry.set_cursor(con, "x:last", "2026-10-28T10:00:00Z")
assert not registry.due(con, "x:last", timedelta(minutes=10), at="2026-10-28T10:09:59Z")
assert registry.due(con, "x:last", timedelta(minutes=10), at="2026-10-28T10:10:00Z")
registry.drop_cursor(con, "x:last")
assert registry.cursor(con, "x:last") is None


# The hook gate: no policy, no pane, or another session means no write and exit 0.
def fire(home, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    e.update(TECHICHI_HOME=str(home), **env)
    data = json.dumps({"session_id": "s1", "cwd": str(home), "source": "startup"})
    r = subprocess.run(["python3", config.HERE / "hooks/session_start.py"], input=data, env=e,
                       capture_output=True, text=True, timeout=5)
    assert r.returncode == 0 and not r.stderr, r
    return (Path(home) / "registry.db").exists()


empty = tempfile.mkdtemp()
assert not fire(empty, HERDR_PANE_ID="w1:p1")  # no policy.toml: no factory on this machine
assert not fire(H)  # no herdr pane
assert not fire(H, HERDR_PANE_ID="w1:p1", HERDR_SESSION="other")
assert fire(H, HERDR_PANE_ID="w1:p1")  # herdr sets no HERDR_SESSION in the default session
os.remove(H / "registry.db")
assert fire(H, HERDR_PANE_ID="w1:p1", HERDR_SESSION="default")

# The skill render: paths filled in, the name from [skills], the extra appended. Worker and planner get links.
(H / "extra").mkdir()
(H / "extra/worker.md").write_text("## Local rules\n\n- The operator is Sam.\n")
pol = {**config.policy(), "skills": {"worker": "my-worker", "planner": "my-planner", "relay": "my-relay", "extra": "extra"}}
with contextlib.redirect_stdout(io.StringIO()):
    install.skills(pol)
w = (H / "skills/my-worker/SKILL.md").read_text()
assert w.startswith("---\nname: my-worker\n") and w.rstrip().endswith("- The operator is Sam.")
assert "{factory}" not in w and "{home}" not in w and f"{H}/registry.db" in w
p = (H / "skills/my-planner/SKILL.md").read_text()
assert f"python3 {config.HERE}/plan.py claim-role" in p and "Local rules" not in p
claude = Path(os.environ["CLAUDE_CONFIG_DIR"])
assert (claude / "skills/my-worker").resolve() == (H / "skills/my-worker").resolve()
assert (claude / "skills/my-planner").is_symlink() and not (claude / "skills/my-relay").exists()  # relay: by path only
with contextlib.redirect_stdout(io.StringIO()):
    install.skills(pol)  # again: the links stay
print("ok")
