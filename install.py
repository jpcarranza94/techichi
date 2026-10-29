#!/usr/bin/env python3
"""Install the factory for this user. Safe to run again. Needs python 3.11 or later for tomllib.

  install.py          hooks, skills and service
  install.py hooks    add the five Recorders to ~/.claude/settings.json, when a harness in use records with hooks
  install.py skills   render the skills into <config>/skills. Link worker and planner for a harness that links skills.
  install.py service  write the launchd plist (macOS) or the systemd user unit (Linux), print how to start it
  install.py plist    the same as service, kept for old notes

Run `install.py skills` again after each core update.
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

import config
import harness

CLAUDE = harness.CLAUDE
HOOKS = config.HERE / "hooks"
PY = sys.executable  # absolute, so the hooks and launchd do not depend on PATH
RECORDERS = [("SessionStart", "*", "session_start.py"), ("SessionEnd", "*", "session_end.py"),
             ("PostToolUse", "Bash", "pr_link.py"),
             ("PostToolUse", "mcp__claude_ai_Slack__slack_send_message", "slack_link.py"),
             ("PostToolUse", "Skill", "ticket_link.py")]
ROLES = ("worker", "planner", "relay")


def hooks(settings=CLAUDE / "settings.json", pol=None):
    if not any(harness.HARNESSES[n]["recorder"] for n in harness.in_use(pol or config.policy())):
        print("skipped: no harness in use records with hooks")
        return
    d = json.loads(settings.read_text()) if settings.exists() else {}
    if settings.exists():
        shutil.copy2(settings, settings.with_suffix(".json.bak-factory"))
    for event, matcher, script in RECORDERS:
        entries = d.setdefault("hooks", {}).setdefault(event, [])
        cmd = f"{PY} {HOOKS / script}"
        if any(str(HOOKS / script) in h.get("command", "") for e in entries for h in e.get("hooks", [])):
            print(f"present: {event} {script}")
            continue
        entries.append({"matcher": matcher, "hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
        print(f"added:   {event} {matcher} -> {cmd}")
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def render(role, pol):
    """The core template with the paths filled in, the name from [skills], and the extra file appended."""
    text = (config.HERE / "skills" / f"techichi-{role}" / "SKILL.md").read_text()
    text = text.replace("{factory}", str(config.HERE)).replace("{home}", str(config.HOME))
    text = re.sub(r"(?m)^name: .*$", f"name: {pol['skills'][role]}", text, count=1)
    extra = pol.get("skills", {}).get("extra")
    if extra:
        f = config.HOME / os.path.expanduser(extra) / f"{role}.md"  # an absolute extra path wins over HOME
        if f.exists():
            text = text.rstrip("\n") + "\n\n" + f.read_text()
    return text


def skills(pol=None):
    pol = pol or config.policy()
    for role in ROLES:
        name = pol['skills'][role]
        out = config.HOME / "skills" / name
        out.mkdir(parents=True, exist_ok=True)
        (out / "SKILL.md").write_text(render(role, pol))
        print(f"rendered: {out / 'SKILL.md'}")
        if role == "relay":
            continue  # the relay gets its skill by path, as a system prompt
        for h in sorted(harness.in_use(pol)):  # an "arg" harness gets the folder on its start line instead
            if harness.HARNESSES[h]["skills"] != "link":
                continue
            link = Path(harness.HARNESSES[h]["skills_dir"]) / name
            if link.is_symlink() or link.exists():
                print(f"present: {link}")
                continue
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(out)
            print(f"linked:  {link} -> {out}")


def fill(template):
    # A service manager gives no PATH. The folders of herdr, git and gh go first, for the policy setup commands too.
    bins = [os.path.dirname(b) for b in (config.HERDR, config.GIT, config.GH) if os.path.isabs(b)]
    path = ":".join(dict.fromkeys(bins + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    text = (config.HERE / template).read_text()
    for k, v in {"{python}": PY, "{core}": str(config.HERE), "{home}": str(config.HOME), "{path}": path}.items():
        text = text.replace(k, v)
    return text


def service(platform=sys.platform):
    """The launchd plist on macOS, the systemd user unit on Linux. Prints the commands that start it."""
    if platform == "darwin":
        out = config.HOME / "techichi.plist"
        out.write_text(fill("launchd/techichi.plist.template"))
        print(f"wrote:   {out}\nStart it:\n  cp {out} ~/Library/LaunchAgents/\n"
              "  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/techichi.plist")
        return out
    out = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user/techichi.service"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(fill("systemd/techichi.service.template"))
    print(f"wrote:   {out}\nStart it:\n  systemctl --user daemon-reload\n  systemctl --user enable --now techichi\n"
          "On a machine with no login session, let the service run without one:\n  loginctl enable-linger $USER")
    return out


if __name__ == "__main__":
    assert sys.version_info >= (3, 11), "run with python 3.11 or later"
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd not in ("all", "hooks", "skills", "service", "plist"):
        sys.exit(__doc__)
    if not config.POLICY.exists():
        sys.exit(f"no policy at {config.POLICY}. Copy policy.example.toml there first.")
    try:
        config.policy()  # a bad policy stops the install here, with one clear line
    except config.PolicyError as e:
        sys.exit(str(e))
    for name, step in (("hooks", hooks), ("skills", skills), ("service", service)):
        if cmd in ("all", name) or (cmd == "plist" and name == "service"):
            step()
