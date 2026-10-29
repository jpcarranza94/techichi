#!/usr/bin/env python3
"""Start one worker Session for one ticket. See DESIGN.md section "Spawn"."""
import argparse
import json
import os
import shutil
import subprocess
import re
import sys
import time
import uuid
from pathlib import Path

import registry



def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__(f"herdr {code}: {message}")
        self.code = code


def herdr(*args):
    """Run herdr. Raise HerdrError on an error reply: on stdout with exit code 0, or on stderr with exit code 1."""
    r = subprocess.run(registry.herdr_cmd() + list(args), capture_output=True, text=True)
    out = json.loads(r.stdout) if r.stdout.strip() else {}
    if r.returncode and "error" not in out:
        try:
            out = json.loads(r.stderr)
        except ValueError:
            pass
    if "error" in out:
        raise HerdrError(out["error"].get("code"), out["error"].get("message"))
    if r.returncode:
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return out.get("result")


def wait_shell(pane, timeout=60):
    """worktree create returns before the pane shell runs. Poll process-info until a shell pid shows."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if isinstance(herdr("pane", "process-info", "--pane", pane)["process_info"].get("shell_pid"), int):
                return True
        except (HerdrError, KeyError, TypeError):
            pass
        time.sleep(1)
    return False


def start_agent(name, pane, sid, timeout=60, args=("--permission-mode", "auto")):
    """agent start, retried on agent_pane_busy for up to timeout seconds. agent_not_ready is not an error."""
    wait_shell(pane, timeout)
    end = time.monotonic() + timeout
    while True:
        try:
            herdr("agent", "start", name, "--kind", "claude", "--pane", pane, "--timeout", "120000",
                  "--", "--session-id", sid, *args)
            return
        except HerdrError as e:
            if e.code == "agent_not_ready":
                print(f"warning: {e}. Waiting for readiness.", file=sys.stderr)
                return
            if e.code != "agent_pane_busy" or time.monotonic() >= end:
                raise
        time.sleep(2)


def wait_ready(pane, timeout=120):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if herdr("agent", "get", pane)["agent"].get("interactive_ready"):
                return True
        except HerdrError:
            pass
        time.sleep(2)
    return False


def copy_worktreeinclude(repo, dest):
    """Copy the git-ignored files that .worktreeinclude lists. Paths stay data, never on a shell line."""
    inc = Path(repo, ".worktreeinclude")
    if not inc.exists():
        return []
    copied = []
    for line in inc.read_text().splitlines():
        rel = line.strip()
        if not rel or rel.startswith("#") or not Path(repo, rel).is_file():
            continue
        if subprocess.run([registry.GIT, "-C", repo, "check-ignore", "-q", rel]).returncode != 0:
            continue
        Path(dest, rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(repo, rel), Path(dest, rel))
        copied.append(rel)
    return copied


def first_prompt(key, start, pol=None):
    checkpoint = "with the plan checkpoint on" if start.get("checkpoint") else "with no plan checkpoint"
    note = f"{start['note']} " if start.get("note") else ""  # extra text for the start skill, from the policy
    return (f"[factory] New ticket {key}. Load the {registry.skill('worker', pol)} skill. "
            f"Then run the {start['skill']} skill for {key} in place, not in worktree mode, {checkpoint}. "
            f"{note}The branch for {key} is already checked out in this directory.")


class ExistingWork(Exception):
    """A branch or an open PR names the key, and no plan row approves the start."""


def key_in(key, text):
    # APP-12 must not match APP-123. Case is free: herdr and the start skill can lowercase a branch.
    return re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(key)}(?![0-9])", text or "") is not None


def existing_work(key, p):
    """Local or remote branches and open PRs that name the key."""
    refs = run([registry.GIT, "-C", p["repo"], "for-each-ref", "--format=%(refname:short)", "refs/heads"]).split()
    refs += [l.split("refs/heads/", 1)[1] for l in
             run([registry.GIT, "-C", p["repo"], "ls-remote", "--heads", "origin"]).splitlines() if "refs/heads/" in l]
    found = [f"branch {b}" for b in dict.fromkeys(refs) if key_in(key, b)]
    prs = json.loads(run([registry.GH, "pr", "list", "-R", p["github"], "--state", "open", "--search", key,
                          "--json", "number,title,headRefName,url"]) or "[]")
    found += [f"PR {x['url']}" for x in prs if key_in(key, x["title"]) or key_in(key, x["headRefName"])]
    return found


def spawn(key, slug=None, pol=None, db=None):
    pol = pol or registry.policy()
    project = key.split("-")[0].upper()
    p = pol["projects"][project]
    name = key.lower()
    branch = f"{key}-{slug}" if slug else key
    sid = str(uuid.uuid4())
    con = registry.connect(db)

    # 0. Pre-check, before any row or worktree. A plan row with hold = 0 means the operator approved the start.
    plan = con.execute("SELECT hold FROM plan WHERE ticket = ?", (key,)).fetchone()
    if not plan:
        found = existing_work(key, p)
        if found:
            raise ExistingWork(", ".join(found)[:400])

    # 1. Identity before execution.
    con.execute("INSERT INTO sessions (session_id, role, project, state) VALUES (?, 'worker', ?, 'registered')",
                (sid, project))
    con.execute("INSERT INTO links (kind, ref, session_id, created_by) VALUES ('ticket', ?, ?, 'spawner')", (key, sid))

    try:
        out = _prepare(key, p, project, name, branch, sid, con, registry.factory(pol)["worktrees"])
    except Exception:
        # No agent started. A dead registered Session holds its ticket Events forever, so end it.
        # The worktree stays for a retry by hand.
        con.execute("UPDATE sessions SET state = 'ended', ended_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                    "WHERE session_id = ?", (sid,))
        raise

    # From here an agent can live in the pane. A failure leaves the Session registered for the daemon.
    pane = out["pane_id"]
    if not wait_ready(pane):
        raise TimeoutError(f"agent in {pane} not ready after 120 s. Session {sid} stays registered.")
    herdr("pane", "report-metadata", pane, "--source", registry.factory(pol)["metadata_source"], "--token", f"ticket={key}")
    herdr("agent", "prompt", pane, first_prompt(key, p["start"], pol))
    return out


def _prepare(key, p, project, name, branch, sid, con, worktrees):
    # 2. Default branch from GitHub.
    base = run([registry.GH, "repo", "view", p["github"], "--json", "defaultBranchRef",
                "-q", ".defaultBranchRef.name"]).strip()
    run([registry.GIT, "-C", p["repo"], "fetch", "--quiet", "origin", base])

    # 3. Worktree, workspace and pane.
    wt = herdr("worktree", "create", "--cwd", p["repo"], "--branch", branch, "--base", f"origin/{base}",
               "--path", str(worktrees / key), "--label", key, "--no-focus")
    pane, ws, cwd = wt["root_pane"]["pane_id"], wt["workspace"]["workspace_id"], wt["worktree"]["path"]
    con.execute("UPDATE sessions SET cwd = ?, branch = ?, pane_id = ?, workspace_id = ? WHERE session_id = ?",
                (cwd, branch, pane, ws, sid))
    con.execute("INSERT INTO links (kind, ref, session_id, created_by) VALUES ('branch', ?, ?, 'spawner')",
                (branch, sid))

    # 4. Ignored files and setup.
    copied = copy_worktreeinclude(p["repo"], cwd)
    for cmd in p.get("setup", []):
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
        if r.returncode:
            # ponytail: a failed setup warns and continues, the worker sees it when it runs the tests.
            print(f"setup failed ({r.returncode}): {cmd}\n{r.stderr[-2000:]}", file=sys.stderr)

    # 5. Agent. spawn() then polls for readiness.
    start_agent(name, pane, sid)
    return {"session_id": sid, "project": project, "branch": branch, "cwd": cwd,
            "pane_id": pane, "workspace_id": ws, "copied": copied}



FACTORY_WS = "factory"  # one herdr workspace holds the planner and the relay, one tab each


def helper_tab(role, cwd):
    """A new tab `role` in the factory workspace, which is made on first use. Returns (pane, workspace, tab)."""
    ws = next((w["workspace_id"] for w in herdr("workspace", "list")["workspaces"] if w.get("label") == FACTORY_WS), None)
    if ws:
        r = herdr("tab", "create", "--workspace", ws, "--cwd", cwd, "--label", role, "--no-focus")
    else:
        r = herdr("workspace", "create", "--cwd", cwd, "--label", FACTORY_WS, "--no-focus")
        herdr("tab", "rename", r["root_pane"]["tab_id"], role)
    p = r["root_pane"]
    return p["pane_id"], p["workspace_id"], p["tab_id"]


def spawn_helper(role, cwd, args, first, db=None):
    """Start a helper Session in a tab `role` of the factory workspace, agent `factory-<role>`. Returns its id."""
    sid = str(uuid.uuid4())
    con = registry.connect(db)
    Path(cwd).mkdir(parents=True, exist_ok=True)
    con.execute("INSERT INTO sessions (session_id, role, cwd, state) VALUES (?, ?, ?, 'registered')", (sid, role, cwd))
    if role == "planner":  # DMs and mentions of the operator go to the planner
        registry.link(con, "inbox", registry.factory()["inbox_ref"], sid, "spawner")
    tab = None
    try:
        pane, ws, tab = helper_tab(role, cwd)
        con.execute("UPDATE sessions SET pane_id = ?, workspace_id = ? WHERE session_id = ?", (pane, ws, sid))
        start_agent(f"factory-{role}", pane, sid, args=args)
    except Exception:
        con.execute("UPDATE sessions SET state = 'ended', ended_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                    "WHERE session_id = ?", (sid,))
        if tab:  # close only this tab. The other helper keeps running in the same workspace.
            try:
                herdr("tab", "close", tab)
            except Exception as e:
                print(f"warning: tab close failed: {e}", file=sys.stderr)
        raise
    if not wait_ready(pane):
        raise TimeoutError(f"{role} in {pane} not ready after 120 s. Session {sid} stays registered.")
    herdr("agent", "prompt", pane, first)
    return sid


def spawn_planner(text, db=None):
    return spawn_helper("planner", str(registry.factory()["helper_cwd"]), ("--permission-mode", "auto"),
                        f"Load the {registry.skill('planner')} skill. " + text, db)


def relay_skill_file(pol=None):
    """The rendered relay skill. install.py writes it. It names the same slack_source.py as relay_args."""
    return registry.HOME / "skills" / registry.skill("relay", pol) / "SKILL.md"


def relay_args():
    """Least privilege for a session that reads text from anyone, external Slack Connect users included.
    --restricted ignores the user settings with their broad Bash allow rules, and it loads no user skills.
    So the skill text comes in as a system prompt, and the herdr SessionStart hook comes in through --settings,
    because the daemon matches the relay by the UUID that this hook reports. dontAsk denies every tool call
    that no rule allows. Verified on 2026-10-28: arbitrary Bash, chained commands, redirects, reads outside
    the cwd, Edit and Write are denied. Real Slack text on the ingest command line was denied too, so ingest
    takes no input from the model: it parses the search results from the relay's own transcript."""
    script = registry.HERE / "slack_source.py"
    skill = relay_skill_file()  # by path: herdr rejects an argument with a newline
    # The herdr Claude integration installs this hook. The relay needs it through --settings.
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    hook = {"hooks": {"SessionStart": [{"matcher": "*", "hooks": [
        {"type": "command", "command": f"bash {claude / 'hooks/herdr-agent-state.sh'} session", "timeout": 5}]}]}}
    return ("--restricted", "--model", "haiku", "--tools", "Bash", "--permission-mode", "dontAsk",
            "--allowedTools", f"Bash(python3 {script} window)", f"Bash(python3 {script} ingest)",
            "mcp__claude_ai_Slack__slack_search_public_and_private", "mcp__claude_ai_Slack__slack_read_thread",
            "--settings", json.dumps(hook), "--append-system-prompt-file", str(skill))


def spawn_relay(text, db=None):
    # The cwd stays empty on purpose: read-only commands are open inside the cwd.
    return spawn_helper("relay", str(registry.factory()["relay_cwd"]), relay_args(), text, db)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("key", help="Jira ticket key, for example APP-1326")
    ap.add_argument("--slug", help="branch suffix: <KEY>-<slug>")
    a = ap.parse_args()
    try:
        print(json.dumps(spawn(a.key.upper(), a.slug), indent=2))
    except subprocess.CalledProcessError as e:
        sys.exit(f"failed: {' '.join(map(str, e.cmd))}\n{e.stderr}")
    except (HerdrError, TimeoutError) as e:
        sys.exit(f"failed: {e}")
