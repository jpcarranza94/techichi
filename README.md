<p align="center"><img src="docs/techichi.svg" alt="Techichi, a small chihuahua sitting" width="180"></p>

# techichi

The techichi was a small dog of ancient Mesoamerica. People think that it is the ancestor of the chihuahua. This techichi herds your coding agents.

BLUF: techichi runs Claude Code sessions for your Jira tickets. It starts one session for each ticket that is ready in your sprint. It sends each Jira, GitHub, and Slack update to the session that owns the work.

The factory is a small Python daemon on macOS. It uses only the Python standard library. herdr runs the sessions. One SQLite file holds the state. The design is in `DESIGN.md`.

## How it works

1. The Jira Source finds a ticket that your `admit` query admits.
2. The spawner makes a git worktree and starts a Claude Code session in a herdr pane.
3. The session runs your start skill for the ticket. It stops after the plan, if you ask for a checkpoint.
4. Comments, reviews, and pushes on the ticket and its pull requests become Events.
5. The daemon sends each Event to its session as one short prompt with a link. The session reads the item at its link.

## Prerequisites

| Item | Version or scope | Why |
|---|---|---|
| macOS | 14 or later | The daemon runs under launchd. |
| Python | 3.11 or later | `tomllib`. No other package. |
| herdr | The fork at `github.com/jpcarranza94/herdr`, version 0.9.0 or later | Panes, worktrees, agent start and prompt, the socket API. The factory was tested with 0.9.0. |
| herdr Claude integration | `herdr integration install claude` | herdr reports the Claude session id of each pane. The daemon matches sessions by this id. |
| Claude Code | 2.1.312 or later | `--session-id`, `--restricted`, `--append-system-prompt-file`, and the permission mode `auto`. |
| GitHub CLI | `gh auth login` done, with the `repo` and `notifications` scopes | The GitHub Source reads notifications. The spawner reads the default branch and open PRs. |
| Jira Cloud token | A scoped API token with `read:jira-work` and `read:jira-user` | The Jira Source reads only. It never writes to Jira. |
| Slack (optional) | The Slack connector of claude.ai in Claude Code, with `slack_search_public_and_private`, `slack_read_thread`, and `slack_send_message` | A relay session reads Slack. Without it, remove `[slack]` from the policy. |
| A start skill | See "Start skill" below | The work on each ticket. |

Claude Code must trust the folders `worktrees` and `helper_cwd` of the policy. Else the trust dialog blocks a new session.

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

3. Edit `~/.config/techichi/policy.toml`. Set your herdr session, your folders, your projects, your Jira site, and your Slack ids. Each key has a comment.

4. Save your Jira token in the file that `[jira] token_file` names. Make it readable only for you:

   ```sh
   touch ~/.config/techichi/jira-token && chmod 600 ~/.config/techichi/jira-token
   ```

   Then paste the token into the file with an editor. Do not put the token on a command line.

5. Install the hooks, the skills, and the plist:

   ```sh
   python3 ~/techichi/install.py
   ```

   This adds five hooks to `~/.claude/settings.json` and keeps a backup next to it. It renders the skills into `~/.config/techichi/skills/` and links the worker and the planner skill into `~/.claude/skills/`. It writes `~/.config/techichi/techichi.plist`.

6. Do one pass by hand. Read the output for errors:

   ```sh
   python3 ~/techichi/daemon.py --once
   ```

7. Start the daemon under launchd:

   ```sh
   cp ~/.config/techichi/techichi.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/techichi.plist
   ```

8. Read the log at `~/.config/techichi/daemon.log`. Each pass writes nothing when there is no work.

To see the factory, run `python3 ~/techichi/factory.py status`.

After you update the repo, run `python3 ~/techichi/install.py skills`. Then restart the daemon:

```sh
launchctl kickstart -k gui/$(id -u)/techichi
```

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
