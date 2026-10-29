"""Check for the config folder: defaults, the herdr socket, the hook gate, and the skill render. Run: python3 test_config.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import contextlib
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

import install
import registry

H = testenv.HOME
assert registry.HOME == H and registry.DB == H / "registry.db" and registry.POLICY == H / "policy.toml"

# Defaults: a policy with no [factory] and no [skills] table.
f = registry.factory({})
assert (f["herdr_session"], f["metadata_source"], f["inbox_ref"]) == ("default", "techichi", "operator")
assert (f["worktrees"], f["helper_cwd"], f["relay_cwd"]) == (H / "worktrees", H, H / "relay")
assert registry.skill("worker", {}) == "techichi-worker" and registry.skill("relay", {}) == "techichi-relay"
# Paths expand, and the policy values win.
f = registry.factory({"factory": {"herdr_session": "work", "worktrees": "~/wt", "inbox_ref": "me"}})
assert f["worktrees"] == Path.home() / "wt" and f["inbox_ref"] == "me"
assert registry.policy()["projects"]["APP"]["repo"] == str(Path.home() / "code/app")  # ~ in a repo expands

# The herdr socket: the default session sits at the top of the herdr folder, a named one in sessions/.
assert registry.socket_path({}) == Path.home() / ".config/herdr/herdr.sock"
assert registry.socket_path({"factory": {"herdr_session": "work"}}) == Path.home() / ".config/herdr/sessions/work/herdr.sock"
assert registry.herdr_cmd({"factory": {"herdr_session": "work"}})[1:] == ["--session", "work"]


# The hook gate: no policy, no pane, or another session means no write and exit 0.
def fire(home, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    e.update(TECHICHI_HOME=str(home), **env)
    data = json.dumps({"session_id": "s1", "cwd": str(home), "source": "startup"})
    r = subprocess.run(["python3", registry.HERE / "hooks/session_start.py"], input=data, env=e,
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
pol = {**registry.policy(), "skills": {"worker": "my-worker", "planner": "my-planner", "relay": "my-relay", "extra": "extra"}}
with contextlib.redirect_stdout(io.StringIO()):
    install.skills(pol)
w = (H / "skills/my-worker/SKILL.md").read_text()
assert w.startswith("---\nname: my-worker\n") and w.rstrip().endswith("- The operator is Sam.")
assert "{factory}" not in w and "{home}" not in w and f"{H}/registry.db" in w
p = (H / "skills/my-planner/SKILL.md").read_text()
assert f"python3 {registry.HERE}/plan.py claim-role" in p and "Local rules" not in p
claude = Path(os.environ["CLAUDE_CONFIG_DIR"])
assert (claude / "skills/my-worker").resolve() == (H / "skills/my-worker").resolve()
assert (claude / "skills/my-planner").is_symlink() and not (claude / "skills/my-relay").exists()  # relay: by path only
with contextlib.redirect_stdout(io.StringIO()):
    install.skills(pol)  # again: the links stay
print("ok")
