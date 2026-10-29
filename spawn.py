#!/usr/bin/env python3
"""Start one worker Session for one ticket, or a helper Session. See DESIGN.md section "Spawn"."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import config
import harness
import herdr
import registry


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout


def end(con, sid):
    con.execute(f"UPDATE sessions SET state = 'ended', ended_at = {registry.NOW} WHERE session_id = ?", (sid,))


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
        if subprocess.run([config.GIT, "-C", repo, "check-ignore", "-q", rel]).returncode != 0:
            continue
        Path(dest, rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(repo, rel), Path(dest, rel))
        copied.append(rel)
    return copied


def first_prompt(key, start, pol):
    checkpoint = "with the plan checkpoint on" if start.get("checkpoint") else "with no plan checkpoint"
    note = f"{start['note']} " if start.get("note") else ""  # extra text for the start skill, from the policy
    return (f"[factory] New ticket {key}. Load the {pol['skills']['worker']} skill. "
            f"Then run the {start['skill']} skill for {key} in place, not in worktree mode, {checkpoint}. "
            f"{note}The branch for {key} is already checked out in this directory.")


class ExistingWork(Exception):
    """A branch or an open PR names the key, and no plan row approves the start."""


def key_in(key, text):
    # APP-12 must not match APP-123. Case is free: herdr and the start skill can lowercase a branch.
    return re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(key)}(?![0-9])", text or "") is not None


def existing_work(key, p):
    """Local or remote branches, and open GitHub PRs, that name the key."""
    refs = run([config.GIT, "-C", p["repo"], "for-each-ref", "--format=%(refname:short)", "refs/heads"]).split()
    refs += [l.split("refs/heads/", 1)[1] for l in
             run([config.GIT, "-C", p["repo"], "ls-remote", "--heads", "origin"]).splitlines() if "refs/heads/" in l]
    found = [f"branch {b}" for b in dict.fromkeys(refs) if key_in(key, b)]
    if "github" not in p:
        return found  # another forge: the branches are enough
    prs = json.loads(run([config.GH, "pr", "list", "-R", p["github"], "--state", "open", "--search", key,
                          "--json", "number,title,headRefName,url"]) or "[]")
    found += [f"PR {x['url']}" for x in prs if key_in(key, x["title"]) or key_in(key, x["headRefName"])]
    return found


def project_of(con, key):
    """The project of a ticket: payload.project of its newest admission Event, else the key prefix (Jira)."""
    r = con.execute("SELECT json_extract(payload, '$.project') FROM events WHERE kind = 'ticket' AND ref = ? "
                    "AND json_extract(payload, '$.admitted') ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
    return (r and r[0]) or key.split("-")[0].upper()


def spawn(key, slug=None, pol=None, db=None):
    pol = pol or config.policy()
    branch = f"{key}-{slug}" if slug else key
    sid = str(uuid.uuid4())
    con = registry.connect(db)
    project = project_of(con, key)
    p = pol["projects"][project]

    # 0. Pre-check, before any row or worktree. A plan row with hold = 0 means the operator approved the start.
    plan = con.execute("SELECT hold FROM plan WHERE ticket = ?", (key,)).fetchone()
    if not plan:
        found = existing_work(key, p)
        if found:
            raise ExistingWork(", ".join(found)[:400])

    # 1. Identity before execution.
    con.execute("INSERT INTO sessions (session_id, role, project, state) VALUES (?, 'worker', ?, 'registered')",
                (sid, project))
    registry.link(con, "ticket", key, sid, "spawner")

    try:
        out = _prepare(key, p, project, branch, sid, con, pol)
    except Exception:
        # No agent started. A dead registered Session holds its ticket Events forever, so end it.
        # The worktree stays for a retry by hand.
        end(con, sid)
        raise

    # From here an agent can live in the pane. A failure leaves the Session registered for the daemon.
    pane = out["pane_id"]
    if not herdr.wait_ready(pane):
        raise TimeoutError(f"agent in {pane} not ready after 120 s. Session {sid} stays registered.")
    herdr.report_ticket(pane, key)
    herdr.prompt(pane, first_prompt(key, p["start"], pol))
    return out


def _prepare(key, p, project, branch, sid, con, pol):
    # 2. Default branch from GitHub, or from the HEAD of origin.
    if "github" in p:
        base = run([config.GH, "repo", "view", p["github"], "--json", "defaultBranchRef",
                    "-q", ".defaultBranchRef.name"]).strip()
    else:  # "ref: refs/heads/main\tHEAD"
        base = run([config.GIT, "-C", p["repo"], "ls-remote", "--symref", "origin", "HEAD"]).split("\t")[0].split("refs/heads/")[1]
    run([config.GIT, "-C", p["repo"], "fetch", "--quiet", "origin", base])

    # 3. Worktree, workspace and pane.
    pane, ws, cwd = herdr.worktree_create(p["repo"], branch, f"origin/{base}", pol["factory"]["worktrees"] / key, key)
    con.execute("UPDATE sessions SET cwd = ?, branch = ?, pane_id = ?, workspace_id = ? WHERE session_id = ?",
                (cwd, branch, pane, ws, sid))
    registry.link(con, "branch", branch, sid, "spawner")

    # 4. Ignored files and setup.
    copied = copy_worktreeinclude(p["repo"], cwd)
    for cmd in p["setup"]:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
        if r.returncode:
            # ponytail: a failed setup warns and continues, the worker sees it when it runs the tests.
            print(f"setup failed ({r.returncode}): {cmd}\n{r.stderr[-2000:]}", file=sys.stderr)

    # 5. Agent of the project's harness. spawn() then polls for readiness. The agent name is the lowercase key.
    name = harness.of_project(pol, project)
    herdr.start_agent(key.lower(), pane, harness.HARNESSES[name]["kind"],
                      harness.start_args(name, sid, pol, [skill_dir(pol, "worker")]))
    return {"session_id": sid, "project": project, "branch": branch, "cwd": cwd,
            "pane_id": pane, "workspace_id": ws, "copied": copied}


def skill_dir(pol, role):
    """The rendered skill of a role. install.py skills writes it."""
    return config.HOME / "skills" / pol["skills"][role]


def spawn_helper(role, cwd, kind_and_args, first, db=None):
    """Start a helper Session in a tab `role` of the helper workspace, agent `factory-<role>`. Returns its id.
    kind_and_args(sid) returns the herdr kind and the agent args for the new session id."""
    pol = config.policy()
    sid = str(uuid.uuid4())
    con = registry.connect(db)
    Path(cwd).mkdir(parents=True, exist_ok=True)
    con.execute("INSERT INTO sessions (session_id, role, cwd, state) VALUES (?, ?, ?, 'registered')", (sid, role, cwd))
    if role == "planner":  # DMs and mentions of the operator go to the planner
        registry.link(con, "inbox", pol["factory"]["inbox_ref"], sid, "spawner")
    tab = None
    try:
        pane, ws, tab = herdr.helper_tab(role, cwd)
        con.execute("UPDATE sessions SET pane_id = ?, workspace_id = ? WHERE session_id = ?", (pane, ws, sid))
        herdr.start_agent(f"factory-{role}", pane, *kind_and_args(sid))
    except Exception:
        end(con, sid)
        if tab:  # close only this tab. The other helper keeps running in the same workspace.
            try:
                herdr.tab_close(tab)
            except Exception as e:
                print(f"warning: tab close failed: {e}", file=sys.stderr)
        raise
    if not herdr.wait_ready(pane):
        raise TimeoutError(f"{role} in {pane} not ready after 120 s. Session {sid} stays registered.")
    herdr.prompt(pane, first)
    return sid


def spawn_planner(text, db=None):
    pol = config.policy()
    name = pol["factory"]["harness"]
    return spawn_helper("planner", str(pol["factory"]["helper_cwd"]),
                        lambda sid: (harness.HARNESSES[name]["kind"],
                                     harness.start_args(name, sid, pol, [skill_dir(pol, "planner")])),
                        f"Load the {pol['skills']['planner']} skill. " + text, db)


def relay_skill_file(pol=None):
    """The rendered relay skill. install.py writes it. It names the same slack_source.py as relay_args."""
    return skill_dir(pol or config.policy(), "relay") / "SKILL.md"


def relay_args():
    """Least privilege for a session that reads text from anyone, external Slack Connect users included.
    --restricted ignores the user settings with their broad Bash allow rules, and it loads no user skills.
    So the skill text comes in as a system prompt, and the herdr SessionStart hook comes in through --settings,
    because the daemon matches the relay by the UUID that this hook reports. dontAsk denies every tool call
    that no rule allows. Verified on 2026-10-28: arbitrary Bash, chained commands, redirects, reads outside
    the cwd, Edit and Write are denied. Real Slack text on the ingest command line was denied too, so ingest
    takes no input from the model: it parses the search results from the relay's own transcript."""
    script = config.HERE / "slack_source.py"
    skill = relay_skill_file()  # by path: herdr rejects an argument with a newline
    # The herdr Claude integration installs this hook. The relay needs it through --settings.
    hook = {"hooks": {"SessionStart": [{"matcher": "*", "hooks": [
        {"type": "command", "command": f"bash {harness.CLAUDE / 'hooks/herdr-agent-state.sh'} session", "timeout": 5}]}]}}
    return ("--restricted", "--model", "haiku", "--tools", "Bash", "--permission-mode", "dontAsk",
            "--allowedTools", f"Bash(python3 {script} window)", f"Bash(python3 {script} ingest)",
            "mcp__claude_ai_Slack__slack_search_public_and_private", "mcp__claude_ai_Slack__slack_read_thread",
            "--settings", json.dumps(hook), "--append-system-prompt-file", str(skill))


def spawn_relay(text, db=None):
    # The relay is Claude Code only: it needs the claude.ai Slack connector and the Claude transcript.
    # The cwd stays empty on purpose: read-only commands are open inside the cwd.
    return spawn_helper("relay", str(config.policy()["factory"]["relay_cwd"]),
                        lambda sid: ("claude", ["--session-id", sid, *relay_args()]), text, db)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("key", help="Jira ticket key, for example APP-1326")
    ap.add_argument("--slug", help="branch suffix: <KEY>-<slug>")
    a = ap.parse_args()
    try:
        print(json.dumps(spawn(a.key.upper(), a.slug), indent=2))
    except subprocess.CalledProcessError as e:
        sys.exit(f"failed: {' '.join(map(str, e.cmd))}\n{e.stderr}")
    except (herdr.HerdrError, TimeoutError) as e:
        sys.exit(f"failed: {e}")
