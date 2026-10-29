"""Offline check for spawn.py: .worktreeinclude copy, first prompt, registry schema. Run: python3 test_spawn.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import herdr  # noqa: E402
import config  # noqa: E402
import subprocess
import tempfile
from pathlib import Path

import registry
import spawn

with tempfile.TemporaryDirectory() as d:
    repo, dest = Path(d, "repo"), Path(d, "wt")
    repo.mkdir(), dest.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    (repo / ".gitignore").write_text("a.env\nsub/\n")
    (repo / ".worktreeinclude").write_text("# comment\na.env\nsub/b.md\ntracked.txt\nmissing.txt\n")
    (repo / "a.env").write_text("X=1")
    (repo / "sub").mkdir()
    (repo / "sub/b.md").write_text("b")
    (repo / "tracked.txt").write_text("t")
    assert spawn.copy_worktreeinclude(str(repo), str(dest)) == ["a.env", "sub/b.md"]
    assert (dest / "sub/b.md").read_text() == "b" and not (dest / "tracked.txt").exists()

    con = registry.connect(Path(d, "r.db"))
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

pol = config.policy()
p = spawn.first_prompt("APP-1", {"skill": "ship-ticket", "checkpoint": True, "note": "Use handoff mode confirm."}, pol)
assert "techichi-worker" in p and "ship-ticket" in p and "checkpoint on" in p and "confirm. The branch for APP-1" in p
assert "handoff" not in spawn.first_prompt("APP-1", {"skill": "ship-ticket"}, pol)

# A stub herdr: an error reply with exit code 0, then readiness on the second agent get.
with tempfile.TemporaryDirectory() as d:
    stub = Path(d, "herdr")
    stub.write_text(f"""#!/bin/sh
case "$3 $4" in
  "agent start") echo '{{"error":{{"code":"agent_not_ready","message":"blocked during startup"}}}}' ;;
  "agent get") if [ -e {d}/seen ]; then r=true; else r=false; touch {d}/seen; fi
               echo '{{"result":{{"agent":{{"interactive_ready":'$r'}}}}}}' ;;
esac
""")
    stub.chmod(0o755)
    config.HERDR = str(stub)
    try:
        herdr.run("agent", "start", "x")
        raise AssertionError("no HerdrError")
    except herdr.HerdrError as e:
        assert e.code == "agent_not_ready"
    herdr.time.sleep = lambda s: None
    assert herdr.wait_ready("wX:p1", timeout=5)

# A stub herdr: no shell pid on the first process-info, agent_pane_busy on the first agent start.
# The real herdr writes that error to stderr and exits 1.
with tempfile.TemporaryDirectory() as d:
    stub = Path(d, "herdr")
    stub.write_text(f"""#!/bin/sh
case "$3 $4" in
  "pane process-info") if [ -e {d}/shell ]; then p=42; else p=null; touch {d}/shell; fi
                       echo '{{"result":{{"process_info":{{"shell_pid":'$p'}}}}}}' ;;
  "agent start") echo x >> {d}/starts
                 if [ "$(wc -l < {d}/starts)" -eq 1 ]; then
                   echo '{{"error":{{"code":"agent_pane_busy","message":"not an available shell"}}}}' >&2; exit 1
                 else echo '{{"result":{{"type":"agent_started"}}}}'; fi ;;
esac
""")
    stub.chmod(0o755)
    config.HERDR = str(stub)
    herdr.start_agent("app-1", "wX:p1", "sid", timeout=5)
    assert Path(d, "shell").exists() and len(Path(d, "starts").read_text().split()) == 2
# Pre-check: branches and open PRs that name the key. APP-12 does not match APP-123.
assert spawn.key_in("APP-12", "app-12-fix") and spawn.key_in("APP-12", "feature/APP-12") and not spawn.key_in("APP-12", "APP-123")
outputs = {"for-each-ref": "main\nAPP-12-old-try\nAPP-123-other\n",
           "ls-remote": "abc\trefs/heads/develop\ndef\trefs/heads/app-12-remote\n",
           "pr": '[{"number": 7, "title": "APP-12: fix", "headRefName": "x", "url": "https://github.com/o/r/pull/7"},'
                 ' {"number": 8, "title": "APP-123", "headRefName": "y", "url": "https://github.com/o/r/pull/8"}]'}
real_run = spawn.run
spawn.run = lambda cmd, **kw: next(v for k, v in outputs.items() if k in cmd)
p = {"repo": "/nowhere", "github": "o/r"}
assert spawn.existing_work("APP-12", p) == ["branch APP-12-old-try", "branch app-12-remote", "PR https://github.com/o/r/pull/7"]

# spawn() raises ExistingWork before any row. A plan row with hold = 0 skips the pre-check.
with tempfile.TemporaryDirectory() as d:
    db = Path(d, "r.db")
    pol = {**config.policy(), "projects": {"APP": p}}
    try:
        spawn.spawn("APP-12", pol=pol, db=db)
        raise AssertionError("no ExistingWork")
    except spawn.ExistingWork as e:
        assert "APP-12-old-try" in str(e)
    con = registry.connect(db)
    assert con.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    con.execute("INSERT INTO plan (ticket, position, hold) VALUES ('APP-12', 1, 0)")
    spawn.existing_work = lambda *a: (_ for _ in ()).throw(AssertionError("pre-check ran"))
    spawn.run = lambda cmd, **kw: (_ for _ in ()).throw(RuntimeError("stop after the pre-check"))
    try:
        spawn.spawn("APP-12", pol=pol, db=db)
    except RuntimeError as e:
        assert "stop after the pre-check" in str(e)
spawn.run = real_run
# The relay: least privilege. Exact allow rules, no user settings, the skill as system prompt.
import contextlib, io, re, install  # noqa: E401,E402
with contextlib.redirect_stdout(io.StringIO()):
    install.skills()
args = spawn.relay_args()
script = config.HERE / "slack_source.py"
for flag in ("--restricted", "--model", "--tools", "--permission-mode", "--settings", "--append-system-prompt-file"):
    assert flag in args
assert args[args.index("--model") + 1] == "haiku"  # mechanical work, Op chose Haiku on 2026-10-28
assert args[args.index("--tools") + 1] == "Bash" and args[args.index("--permission-mode") + 1] == "dontAsk"
allowed = list(args[args.index("--allowedTools") + 1:args.index("--settings")])
assert allowed == [f"Bash(python3 {script} window)", f"Bash(python3 {script} ingest)",
                   "mcp__claude_ai_Slack__slack_search_public_and_private", "mcp__claude_ai_Slack__slack_read_thread"]
skill = open(args[args.index("--append-system-prompt-file") + 1]).read()
# The rendered skill names exactly the slack_source.py of the allow rules, so the relay may run its commands.
assert set(re.findall(r"python3 (\S+/slack_source\.py) (\w+)", skill)) == {(str(script), "window"), (str(script), "ingest")}
assert "{factory}" not in skill and args[args.index("--append-system-prompt-file") + 1] == str(spawn.relay_skill_file())
assert not any("\n" in a for a in args)  # herdr: "agent arguments cannot be encoded safely for the target shell"
assert "herdr-agent-state.sh session" in args[args.index("--settings") + 1]

# Helpers share one workspace "factory", one tab each. A failed start closes only its tab.
calls, real_run, real_start = [], herdr.run, herdr.start_agent
def fake_herdr(*a, existing=None):
    calls.append(a)
    if a[:2] == ("workspace", "list"):
        return {"workspaces": [{"workspace_id": "w14", "label": "factory"}] if fake_herdr.existing else []}
    if a[:2] == ("tab", "create"):
        return {"root_pane": {"pane_id": "w14:p3", "workspace_id": "w14", "tab_id": "w14:t3"}}
    if a[:2] == ("workspace", "create"):
        return {"root_pane": {"pane_id": "w20:p1", "workspace_id": "w20", "tab_id": "w20:t1"}}
    return {}
herdr.run = fake_herdr
fake_herdr.existing = True
assert herdr.helper_tab("relay", "/tmp/r") == ("w14:p3", "w14", "w14:t3")
assert calls[-1] == ("tab", "create", "--workspace", "w14", "--cwd", "/tmp/r", "--label", "relay", "--no-focus")
fake_herdr.existing, calls[:] = False, []
assert herdr.helper_tab("planner", "/tmp/p") == ("w20:p1", "w20", "w20:t1")
assert calls[1][:5] == ("workspace", "create", "--cwd", "/tmp/p", "--label") and calls[1][5] == "factory"
assert calls[2] == ("tab", "rename", "w20:t1", "planner")
fake_herdr.existing, calls[:] = True, []
def failing_start(*a, **k):
    raise RuntimeError("agent start failed")
herdr.start_agent = failing_start
try:
    spawn.spawn_helper("relay", tempfile.mkdtemp(), (), "x", db=Path(tempfile.mkdtemp(), "h.db"))
    raise AssertionError("spawn_helper must raise")
except RuntimeError:
    pass
assert ("tab", "close", "w14:t3") in calls and not any(c[:2] == ("workspace", "close") for c in calls)
herdr.run, herdr.start_agent = real_run, real_start

print("ok")
