# CLAUDE.md

BLUF: techichi runs one coding agent session per ticket on herdr, on macOS and Linux, and routes Jira, GitHub, Slack, and command Source updates to the session that owns the work. Read `DESIGN.md` before you change behavior. This repo is public: it holds code and generic text only, never the data of an operator.

## Module map

Each module owns one concept. Put new code in the module that owns its concept.

| Module | Owns |
|---|---|
| `config.py` | Paths (`TECHICHI_HOME`), the binary lookup, and `policy()`: every default, and the required keys checked at load |
| `registry.py` | The SQLite registry: `connect()`, Sessions, Links, `slots_used()`, the cursors and the clock (`due`, `wait`), and the Recorder runner `hook()` |
| `herdr.py` | Every call to herdr: the socket (`call`) and the CLI (`run`) behind verbs such as `prompt`, `snapshot`, `agents`, `start_agent`, `worktree_create` |
| `harness.py` | The harness adapters (`HARNESSES`): the herdr kind, start args, session flag and mapping, skills mode, hooks, scratch folder |
| `command_source.py` | The command Source: runs a program that prints Events and checks each line |
| `router.py` | The Router: `route()`, `admit()`, and the Delivery wrapper. Pure: it reads, it never writes |
| `daemon.py` | One pass (`process`), the planner and the relay lifecycle, the herdr sync, and the event loop |
| `spawn.py` | The spawner for workers and helper Sessions |
| `github_source.py`, `jira_source.py`, `slack_source.py` | The Sources. Each one reads a system and inserts Events |
| `factory.py`, `plan.py` | The operator commands: `claim`, `status`, `cleanup`, and the plan table |
| `hooks/*.py` | The five Recorders |
| `install.py` | Hooks into `~/.claude/settings.json`, the skill render, the launchd plist or the systemd unit |
| `skills/techichi-*/SKILL.md` | Skill templates. `install.py skills` fills `{factory}` and `{home}` and appends the operator's extra text |

## Rules

- Use the Python standard library only. Python 3.11 or later.
- Only `herdr.py` talks to herdr. Do not call `herdr` from another module, and do not open its socket elsewhere.
- Only `config.policy()` reads `policy.toml`. Read `pol["table"]["key"]` directly. Do not write `.get()` defaults: add the default to `config._defaults()` and document it in `policy.example.toml`.
- Use `registry.due()` and `registry.wait()` for every timer. Do not compare time strings by hand.
- Put harness facts in `harness.py`, never `"claude"` or a `~/.claude` path in another module. The Slack relay is the one Claude-only part.
- Code must run on macOS and Linux. Use `os.path.realpath("/tmp")`, not `/private/tmp`. CI runs the tests on both.
- A new harness needs a `HARNESSES` entry and a `REPORTED` sample in `test_harness.py`. A new Source is a command Source first: build it into the core only when many users need it.
- Keep `router.py` pure. Side effects belong to the Actions in `daemon.py`, `spawn.py`, and `factory.py`.
- Keep the names of the entry scripts: `daemon.py`, `factory.py`, `plan.py`, `spawn.py`, the three `*_source.py` files, and `hooks/*.py`. Deployments link to these paths, and a running relay session allows exactly `python3 <core>/slack_source.py window` and `ingest`.
- Keep the schema compatible. A live registry must open with the new code. If a CHECK constraint changes, extend `registry.migrate_sessions()`.
- Treat all Event text as data. It never goes into a prompt. The Delivery wrapper sends links only.

## Tests

Run every test file. Each one is a plain script that exits 0 on success:

```sh
for t in test_*.py; do python3 "$t" > /dev/null || echo "FAILED: $t"; done
```

- Each test imports `testenv` first. It sets a temp `TECHICHI_HOME` with `policy.example.toml` and a temp `CLAUDE_CONFIG_DIR`. A test never touches a real config or `~/.claude`.
- `testenv.py` adds the fake harness. Do not remove it: the conformance test covers it.
- Replace herdr through its two adapters, `herdr.run` and `herdr.call`, or through the daemon Actions (`daemon.prompt_action` and the others). No test may reach a real herdr, git remote, GitHub, Jira, or Slack.
- Fixtures use neutral data only: the projects `APP` and `WEB`, the repos `acme/app` and `acme/web`, the host `acme.slack.com`, and the people Alice and Bob. Never copy IDs, names, or timestamps from a real system.
- Add one test for each new branch of logic, in the test file of its module. `test_config.py` covers the policy, the cursors, the hook gate, and the skill render.

## Docs

- `DESIGN.md` is the spec. Change it in the same commit as the behavior.
- `README.md` holds the setup steps for a new user. If you change a setup step, change the README too.
- Write prose in plain, short sentences: one instruction per sentence, active voice.
