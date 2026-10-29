"""Check for the install on macOS and Linux: the service file, and a fresh install with no Jira, no GitHub, and a fake herdr.
The daemon must do a pass and write no file outside its config folder. Run: python3 test_install.py"""
import testenv  # noqa: F401  (first: a temp config folder)
import contextlib
import io
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import config
import install

CORE = config.HERE

# --- The service file for each OS. On Linux with systemd, systemd-analyze checks the unit. ---
xdg = Path(tempfile.mkdtemp())
os.environ["XDG_CONFIG_HOME"] = str(xdg)
with contextlib.redirect_stdout(io.StringIO()) as o:
    plist = install.service("darwin")
    unit = install.service("linux")
assert plist == config.HOME / "techichi.plist" and unit == xdg / "systemd/user/techichi.service"
for f in (plist, unit):
    text = f.read_text()
    assert "{" not in text.replace("{{", ""), f"unfilled placeholder in {f}"
    assert f"{CORE}/daemon.py" in text and str(config.HOME) in text and sys.executable in text
assert "ExecStart=" in unit.read_text() and "Restart=always" in unit.read_text()
assert "launchctl bootstrap" in o.getvalue() and "systemctl --user enable --now techichi" in o.getvalue()
if sys.platform.startswith("linux") and shutil.which("systemd-analyze"):
    r = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

# --- A fresh install: a temp HOME, a policy with one command Source, no GitHub, no gh, no Jira, no Slack ---
home = Path(tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None)).resolve()  # a short socket path
cfg = home / ".config/techichi"
cfg.mkdir(parents=True)
policy = (CORE / "policy.example.toml").read_text()
policy = policy[:policy.index("[jira]")] + policy[policy.index("[cleanup]"):]  # the README: remove what you do not use
cmd = [sys.executable, "-c", "print('{\"id\": \"e1\", \"kind\": \"ticket\", \"ref\": \"APP-1\", \"payload\": {}}')"]
policy = re.sub(r"(?m)^github .*\n", "", policy)  # the projects are on another git host
policy = policy.replace("[factory]\n", "[factory]\nsources = [\"tickets\"]\n", 1)
policy += f"\n[sources.tickets]\ncommand = {json.dumps(cmd)}\n"
(cfg / "policy.toml").write_text(policy)
env = {k: v for k, v in os.environ.items() if k not in ("TECHICHI_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME")}
env.update(HOME=str(home), TECHICHI_GH="/nonexistent/gh")  # gh is not installed
(home / ".claude").mkdir()
r = subprocess.run([sys.executable, CORE / "install.py"], env=env, capture_output=True, text=True)
assert r.returncode == 0, r.stderr
assert (cfg / "skills/techichi-worker/SKILL.md").exists() and (home / ".claude/skills/techichi-worker").is_symlink()
assert json.loads((home / ".claude/settings.json").read_text())["hooks"]["SessionStart"]

# The fake herdr: an empty snapshot on the socket of the default session.
sock = home / ".config/herdr/herdr.sock"
sock.parent.mkdir(parents=True)
methods = []


class Herdr(socketserver.StreamRequestHandler):
    def handle(self):
        req = json.loads(self.rfile.readline())
        methods.append(req["method"])
        result = {"snapshot": {"agents": []}} if req["method"] == "session.snapshot" else {}
        self.wfile.write((json.dumps({"id": req["id"], "result": result}) + "\n").encode())


server = socketserver.ThreadingUnixStreamServer(str(sock), Herdr)
threading.Thread(target=server.serve_forever, daemon=True).start()
before = {p: p.stat().st_mtime for p in home.rglob("*") if p.is_file()}
time.sleep(1.1)  # a write in the pass gets a newer mtime
r = subprocess.run([sys.executable, CORE / "daemon.py", "--once"], env=env, capture_output=True, text=True, timeout=60)
server.shutdown()
assert r.returncode == 0, r.stderr + r.stdout
assert "poll failed" not in r.stdout and "Traceback" not in r.stderr, r.stdout + r.stderr
assert methods == ["session.snapshot"]
written = [p for p in home.rglob("*") if p.is_file() and p != sock and before.get(p) != p.stat().st_mtime]
assert written and all(cfg in p.parents for p in written), written
import registry  # noqa: E402
con = registry.connect(cfg / "registry.db")
assert [tuple(r) for r in con.execute("SELECT id, source, status FROM events")] == [("tickets:e1", "tickets", "unrouted")]
print("ok")
