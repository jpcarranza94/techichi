"""herdr: the only module that talks to herdr. Callers use the verbs below, never a herdr command or method.

Two transports sit behind the verbs: the socket (call) and the CLI (run). Each verb keeps the transport that
the factory used before this module existed. Tests replace call and run, the two adapters.
"""
import json
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import config


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__(f"herdr {code}: {message}")
        self.code = code


def session():
    return config.policy()["factory"]["herdr_session"]


def socket_path(name=None):
    """The socket of the session. herdr keeps the default session at the top of its folder."""
    name = name or session()
    return Path.home() / ".config/herdr" / ("herdr.sock" if name == "default" else f"sessions/{name}/herdr.sock")


# --- The two adapters ----------------------------------------------------------------------------------------

def call(method, params=None):
    """One socket request on its own connection. A subscribed connection carries only events."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(str(socket_path()))
        s.sendall((json.dumps({"id": f"factory:{uuid.uuid4()}", "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    out = json.loads(buf)
    if "error" in out:
        raise HerdrError(out["error"].get("code"), out["error"].get("message"))
    return out["result"]


def run(*args):
    """The herdr CLI. Raise HerdrError on an error reply: on stdout with exit code 0, or on stderr with exit code 1."""
    r = subprocess.run([config.HERDR, "--session", session(), *args], capture_output=True, text=True)
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


# --- Agents and panes ----------------------------------------------------------------------------------------

def session_id(agent):
    """The Claude session UUID of a herdr agent record, or None."""
    return (agent.get("agent_session") or {}).get("value")


def snapshot():
    return call("session.snapshot")["snapshot"]


def prompt(pane, text):
    call("agent.prompt", {"target": pane, "text": text})


def report_ticket(pane, key):
    """The pane token ticket=KEY. herdr forgets it on a restart, so the daemon applies it again."""
    call("pane.report_metadata", {"pane_id": pane, "source": config.policy()["factory"]["metadata_source"],
                                  "tokens": {"ticket": key}})


def agents():
    return run("agent", "list")["agents"]


def agent(target):
    """The agent record of a pane id or an agent name."""
    return run("agent", "get", target)["agent"]


def find_agent(target):
    """The agent record of a pane id, an agent name, or a Claude session UUID."""
    if UUID.fullmatch(target):
        for a in agents():
            if session_id(a) == target:
                return a
        raise ValueError(f"no herdr agent has session {target}")
    return agent(target)


def wait_shell(pane, timeout=60):
    """worktree create returns before the pane shell runs. Poll process-info until a shell pid shows."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if isinstance(run("pane", "process-info", "--pane", pane)["process_info"].get("shell_pid"), int):
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
            run("agent", "start", name, "--kind", "claude", "--pane", pane, "--timeout", "120000",
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
            if agent(pane).get("interactive_ready"):
                return True
        except HerdrError:
            pass
        time.sleep(2)
    return False


# --- Worktrees, workspaces and tabs --------------------------------------------------------------------------

def worktree_create(repo, branch, base, path, label):
    """A worktree with its own workspace and pane. Returns (pane_id, workspace_id, cwd)."""
    wt = run("worktree", "create", "--cwd", repo, "--branch", branch, "--base", base,
             "--path", str(path), "--label", label, "--no-focus")
    return wt["root_pane"]["pane_id"], wt["workspace"]["workspace_id"], wt["worktree"]["path"]


def worktree_remove(workspace):
    """Remove the worktree of a workspace. Never with --force. herdr closes the workspace too."""
    run("worktree", "remove", "--workspace", workspace)


def workspaces():
    return run("workspace", "list")["workspaces"]


def workspace_close(workspace):
    run("workspace", "close", workspace)


HELPERS = "factory"  # one workspace holds the planner and the relay, one tab each


def helper_tab(role, cwd):
    """A new tab `role` in the helper workspace, which is made on first use. Returns (pane, workspace, tab)."""
    ws = next((w["workspace_id"] for w in workspaces() if w.get("label") == HELPERS), None)
    if ws:
        r = run("tab", "create", "--workspace", ws, "--cwd", cwd, "--label", role, "--no-focus")
    else:
        r = run("workspace", "create", "--cwd", cwd, "--label", HELPERS, "--no-focus")
        run("tab", "rename", r["root_pane"]["tab_id"], role)
    p = r["root_pane"]
    return p["pane_id"], p["workspace_id"], p["tab_id"]


def tab_close(tab):
    run("tab", "close", tab)


def subscribe(types):
    """A socket that streams the events of `types`, one JSON line each. The caller reads it and closes it."""
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(socket_path()))
    s.sendall((json.dumps({"id": "factory:subscribe", "method": "events.subscribe",
                           "params": {"subscriptions": [{"type": t} for t in types]}}) + "\n").encode())
    return s
