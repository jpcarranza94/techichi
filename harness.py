"""Harness adapters: how the factory starts a coding agent and knows its session. See DESIGN.md "Harnesses".

One dict entry per harness. The policy picks one by name: [factory] harness, or [projects.X] harness.
Every entry must pass test_harness.py, the conformance test.
"""
import os
import re
from pathlib import Path

CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")  # the Claude Code home

# The kinds that `herdr agent start --kind` accepts (herdr docs, cli-reference).
HERDR_KINDS = {"pi", "claude", "codex", "gemini", "cursor", "devin", "agy", "cline", "omp", "mastracode", "opencode",
               "copilot", "kimi", "kiro", "droid", "amp", "grok", "hermes", "kilo", "qodercli", "qwen", "letta", "maki",
               "muse"}

HARNESSES = {
    "claude": {
        "kind": "claude",
        "args": ["--permission-mode", "auto"],
        "session": "--session-id",       # the spawner picks the session id before the agent starts
        "skills": "link",                # install.py links the worker and planner skills into skills_dir
        "skills_dir": CLAUDE / "skills",
        "recorder": True,                # install.py hooks adds the Claude Code hooks
        "scratch": "claude-{uid}/{slug}/{sid}/",  # under the real /tmp: where the agent makes its own worktrees
    },
    "pi": {
        "kind": "pi",
        "args": [],
        "session": "--session-id",       # "Use exact project session ID, creating it if missing"
        "skills": "arg",                 # the rendered skill folder goes on the start line, nothing in ~/.pi
        "skill_flag": "--skill",
        "recorder": False,               # no hooks: the daemon derives the PR Link from the branch
        "scratch": None,
    },
}


def of_project(pol, project):
    """The harness name of a project, or the factory default."""
    return (pol["projects"].get(project) or {}).get("harness") or pol["factory"]["harness"]


def in_use(pol):
    """The harness names that the policy uses: the default and each project's."""
    return {pol["factory"]["harness"]} | {of_project(pol, k) for k in pol["projects"]}


def kinds(pol):
    """The herdr agent kinds that sync matches to Sessions."""
    return {HARNESSES[n]["kind"] for n in in_use(pol)}


def start_args(name, sid, pol, skill_dirs=()):
    """The agent arguments after `--`: the session flag and id first, then the harness and policy args."""
    h = HARNESSES[name]
    args = [h["session"], sid, *h["args"], *pol.get("harness", {}).get(name, {}).get("args", [])]
    if h["skills"] == "arg":
        for d in skill_dirs:
            args += [h["skill_flag"], str(d)]
    return args


def session_id(agent):
    """The session id of a herdr agent record, or None. herdr reports an id, or a path to a session file.
    A path names its session as <time>_<id>.jsonl (Pi), so the id is the part after the last _."""
    ref = agent.get("agent_session") or {}
    value = ref.get("value")
    if value and ref.get("kind") == "path":
        return Path(value).stem.rsplit("_", 1)[-1]
    return value


def scratch_dir(name, cwd, sid):
    """The folder where the harness makes its own worktrees for a Session, or None."""
    pattern = HARNESSES[name]["scratch"]
    if not pattern:
        return None
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
    # The real /tmp: /private/tmp on macOS, /tmp on Linux.
    return os.path.join(os.path.realpath("/tmp"), pattern.format(uid=os.getuid(), slug=slug, sid=sid))
