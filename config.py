"""The config: where things are, and the policy with every default filled in. See DESIGN.md section "Policy".

policy() is the only reader of policy.toml. It fails at load with one clear line, never later in a daemon pass.
"""
import os
import shutil
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent  # the code: schema.sql and the skill templates
# The config folder: policy.toml, registry.db and the rendered skills. Hooks get no env, so the default matters.
HOME = Path(os.path.expanduser(os.environ.get("TECHICHI_HOME", "~/.config/techichi"))).resolve()
DB = HOME / "registry.db"
POLICY = HOME / "policy.toml"
# launchd gives no PATH. TECHICHI_HERDR, TECHICHI_GIT and TECHICHI_GH override the lookup.
HERDR, GIT, GH = (os.environ.get(f"TECHICHI_{n.upper()}") or shutil.which(n) or n for n in ("herdr", "git", "gh"))


class PolicyError(ValueError):
    pass


def _defaults():
    return {
        "factory": {"herdr_session": "default", "metadata_source": "techichi", "inbox_ref": "operator",
                    "worktrees": HOME / "worktrees", "helper_cwd": HOME, "relay_cwd": HOME / "relay"},
        "skills": {"worker": "techichi-worker", "planner": "techichi-planner", "relay": "techichi-relay"},
        "route": {"ticket": ["coordinator", "worker"], "pr": ["worker", "coordinator"],
                  "slack": ["worker", "coordinator"], "inbox": ["planner"]},
        "hold": {"notify_after_minutes": 60},
        "spawn": {"max_active": 0, "gap_seconds": 30},  # 0: nothing spawns until the policy sets a cap
        "cleanup": {"done_statuses": ["Done"]},
    }


SLACK = {"poll_minutes": 5, "max_poll_minutes": 30, "inbox": False}  # [slack] is optional. Without it no relay starts.
REQUIRED = {"jira": ("api", "site", "email", "token_file"), "slack": ("me", "channels", "hosts"),
            "project": ("repo", "github", "admit", "start")}


def complete(raw, where="policy"):
    """The policy with every default filled in and the required keys checked. Raise PolicyError with one line."""
    pol = {k: dict(v) if isinstance(v, dict) else v for k, v in raw.items()}
    for table, values in _defaults().items():
        pol[table] = {**values, **pol.get(table, {})}
    if "slack" in pol:
        pol["slack"] = {**SLACK, **pol["slack"]}
    f = pol["factory"]
    for k in ("worktrees", "helper_cwd", "relay_cwd"):
        f[k] = Path(os.path.expanduser(f[k]))

    def need(table, keys, name):
        missing = [k for k in keys if k not in table]
        if missing:
            raise PolicyError(f"{where}: [{name}] needs {', '.join(missing)}")

    if not pol.get("projects"):
        raise PolicyError(f"{where}: no [projects.<KEY>] table")
    for key, p in pol["projects"].items():
        need(p, REQUIRED["project"], f"projects.{key}")
        need(p["start"], ("skill",), f"projects.{key}.start")
        p["repo"] = os.path.expanduser(p["repo"])
        p.setdefault("setup", [])
    need(pol.get("jira", {}), REQUIRED["jira"], "jira")
    if "slack" in pol:
        need(pol["slack"], REQUIRED["slack"], "slack")
    return pol


def policy(path=None):
    path = Path(path or POLICY)
    return complete(tomllib.loads(path.read_text()), str(path))
