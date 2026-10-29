<p align="center"><img src="docs/techichi.svg" alt="Techichi, a small chihuahua sitting" width="180"></p>

# techichi

The techichi was a small dog of ancient Mesoamerica. People think that it is the ancestor of the chihuahua. This techichi herds your coding agents.

BLUF: techichi runs coding agent sessions for your tickets. It starts one session for each ticket that is ready. It sends each update from Jira, GitHub, Slack, or your own Source to the session that owns the work.

The factory is a small Python daemon for macOS and Linux. It uses only the Python standard library. herdr runs the sessions. The coding agent is Claude Code or Pi. One SQLite file holds the state. The design is in `DESIGN.md`.

## How it works

1. A Source finds a ticket that is ready: your Jira `admit` query, a labeled GitHub issue, or your own command.
2. The spawner makes a git worktree and starts a coding agent session in a herdr pane.
3. The session runs your start skill for the ticket. It stops after the plan, if you ask for a checkpoint.
4. Comments, reviews, and pushes on the ticket and its pull requests become Events.
5. The daemon sends each Event to its session as one short prompt with a link. The session reads the item at its link.

## Prerequisites

| Item | Version or scope | Why |
|---|---|---|
| macOS or Linux | macOS 14 or later, or Linux with systemd | The daemon runs under launchd or as a systemd user service. |
| Python | 3.11 or later | `tomllib`. No other package. |
| herdr | The fork at `github.com/jpcarranza94/herdr`, version 0.9.0 or later | Panes, worktrees, agent start and prompt, the socket API. The factory was tested with 0.9.0. |
| A coding agent | Claude Code 2.1.312 or later, or Pi 1.4.0 or later | See "Harness" below. |
| herdr integration of that agent | `herdr integration install claude` or `herdr integration install pi` | herdr reports the session id of each pane. The daemon matches sessions by this id. |
| GitHub CLI | `gh auth login` done, with the `repo` and `notifications` scopes | The GitHub Source reads notifications. The spawner reads the default branch and open PRs. |
| Jira Cloud token (optional) | A scoped API token with `read:jira-work` and `read:jira-user` | The Jira Source reads only. Without Jira, remove `[jira]` from the policy. |
| Slack (optional) | Claude Code with the Slack connector of claude.ai: `slack_search_public_and_private`, `slack_read_thread`, `slack_send_message` | A relay session reads Slack. It needs Claude Code. Without it, remove `[slack]` from the policy. |
| A start skill | See "Start skill" below | The work on each ticket. |

The coding agent must trust the folders `worktrees` and `helper_cwd` of the policy. Else a trust dialog blocks a new session.

## Setup

Do these steps in order. Run the commands in a shell inside herdr.

1. Clone the repo:

   ```sh
   git clone git@github.com:<you>/techichi.git ~/techichi
   ```

2. Make the config folder and copy the example policy:

   ```sh
   mkdir -p ~/.config/techichi
   cp ~/techichi/policy.example.toml ~/.config/techichi/policy.toml
   ```

3. Edit `~/.config/techichi/policy.toml`. Set your herdr session, your harness, your folders, your projects, and your Sources. Remove `[jira]` or `[slack]` if you do not use them. Each key has a comment.

4. If you use Jira, save your Jira token in the file that `[jira] token_file` names. Make it readable only for you:

   ```sh
   touch ~/.config/techichi/jira-token && chmod 600 ~/.config/techichi/jira-token
   ```

   Then paste the token into the file with an editor. Do not put the token on a command line.

5. Install the hooks, the skills, and the service:

   ```sh
   python3 ~/techichi/install.py
   ```

   It renders the skills into `~/.config/techichi/skills/`. For Claude Code, it also adds five hooks to `~/.claude/settings.json`, keeps a backup next to it, and links the worker and the planner skill into `~/.claude/skills/`. Pi gets the skill folder on its start line, so nothing goes into `~/.pi`. On macOS it writes `~/.config/techichi/techichi.plist`. On Linux it writes `~/.config/systemd/user/techichi.service`. A bad policy stops the install with one line that names the missing key.

6. Do one pass by hand. Read the output for errors:

   ```sh
   python3 ~/techichi/daemon.py --once
   ```

7. Start the daemon. On macOS:

   ```sh
   cp ~/.config/techichi/techichi.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/techichi.plist
   ```

   On Linux:

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now techichi
   loginctl enable-linger $USER   # only on a machine with no login session, for example a server
   ```

8. Read the log. On macOS it is `~/.config/techichi/daemon.log`. On Linux, run `journalctl --user -u techichi -f`. Each pass writes nothing when there is no work.

To see the factory, run `python3 ~/techichi/factory.py status`.

After you update the repo, run `python3 ~/techichi/install.py skills`. Then restart the daemon: `launchctl kickstart -k gui/$(id -u)/techichi` on macOS, or `systemctl --user restart techichi` on Linux.

## Harness

The harness is the coding agent that works on a ticket. `[factory] harness` sets it for all projects, and `[projects.X] harness` sets it for one project.

| Harness | Session id | Instructions | Hooks | Notes |
|---|---|---|---|---|
| `claude` | `--session-id`, picked by techichi | skills linked into `~/.claude/skills` | yes: five Recorders | The Slack relay needs it. |
| `pi` | `--session-id`, picked by techichi | `--skill <folder>` on the start line | no | Pi works with many model providers. Set the model with `[harness.pi] args`. |

Without hooks, techichi links a pull request to a session by its head branch: the branch that the spawner made for the ticket. Open the PR from that branch.

To add a harness, add one entry to `HARNESSES` in `harness.py`. `test_harness.py` checks every entry: its herdr kind, its session flag, its instructions, and how herdr reports its session.

## Sources

`[factory] sources` lists the Sources. The default is `github`, plus `jira` and `slack` when their tables exist.

- `github`: activity on the pull requests of your project repos.
- `jira`: tickets that your `admit` query admits, and updates on the tickets of your sessions.
- `slack`: the relay. It needs Claude Code and the claude.ai Slack connector.
- Any other name is a command Source. Add `[sources.<name>] command = [...]`. techichi runs the command every `every_seconds` (default 60). The command prints one JSON Event per line:

  ```json
  {"id": "42", "kind": "ticket", "ref": "APP-42", "payload": {"what": "ready", "actor": "bot", "at": "2026-10-29T10:00:00Z", "url": "https://...", "admitted": true, "project": "APP", "slug": "add-export"}}
  ```

  - `kind` is `ticket`, `pr`, `slack`, or `inbox`.
  - `admitted: true` on a `ticket` starts work. The ref must look like `APP-42`, and `project` names a project of the policy.
  - The env holds `TECHICHI_SINCE`: the start time of the last good run.
  - A bad line is skipped. A failed run is tried again after `every_seconds`.

  `examples/github_issues.py` starts work on labeled GitHub issues, with no Jira. Write your own command for Telegram, GitLab, Linear, or any other service, in any language.

Without Jira, the cleanup of a finished session needs no ticket status: the merged or closed pull requests, one hour idle, and a clean checkout decide.

## Start skill

The start skill does the work on one ticket. You write it, or you use one that you have. `[projects.X] start` names it.

The contract:

- It takes the ticket key as its argument, for example `APP-12`.
- It works in the current folder, on the branch that is checked out. The spawner made the worktree and the branch. The skill must not make another worktree.
- If the prompt says "with the plan checkpoint on", it writes a plan, shows it, and stops. It continues only when you approve.
- It opens each pull request with `gh pr create`. A hook reads the PR URL from that command and links the PR to the session.
- It needs no question to start. If it has one, answer it with `start.note` in the policy.

The spawner sends this first prompt:

```
[factory] New ticket APP-12. Load the techichi-worker skill. Then run the ship-ticket skill for APP-12 in place, not in worktree mode, with the plan checkpoint on. The branch for APP-12 is already checked out in this directory.
```

A minimal start skill, in `~/.claude/skills/ship-ticket/SKILL.md`:

```markdown
---
name: ship-ticket
description: Take one Jira ticket to a pull request in the current folder. Use when a prompt says to run ship-ticket for a ticket key.
---

# Ship ticket

1. Read the ticket with the Atlassian MCP tools: summary, description, acceptance criteria, comments.
2. Read the code that the ticket touches.
3. Write a plan to `docs/plan/<KEY>.md`: the changes for each file, the tests, and the risks.
4. If the prompt says "with the plan checkpoint on", show the plan and stop. Wait for approval.
5. Implement the plan. Run the tests of the repo. Commit.
6. Push the branch. Open the pull request with `gh pr create`. Put the key in the title.
7. Write the PR URL as your last message.
```

## Safety

- The Sources only read. The Jira Source uses GET requests. The GitHub Source never marks a notification as read.
- The prompt to a session holds only a link, never the text of a comment.
- The relay session reads Slack text from anyone. It runs with `--restricted` and `--permission-mode dontAsk`, and it may run only two commands.
- The daemon never removes a worktree that has changes or commits that no remote has.

## Tests

Run each test file. Each one uses a temp config folder:

```sh
cd ~/techichi && for t in test_*.py; do python3 "$t" || echo "FAILED: $t"; done
```

## License

MIT. See `LICENSE`.
