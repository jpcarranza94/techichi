#!/usr/bin/env python3
"""The command Source: any executable that prints Events, one JSON object per line. See DESIGN.md "Sources".

  [factory] sources = ["github", "tickets"]
  [sources.tickets]
  command = ["python3", "/path/to/my_events.py"]
  every_seconds = 60       # default 60
  timeout_seconds = 60     # default 60

The command gets TECHICHI_SINCE: the start time of its last good run, or an empty string on the first run.
Each line is {"id", "kind", "ref", "payload": {"what", "actor", "at", "url", "self", "admitted", "project", "slug"}}.
An admitted ticket needs a key-shaped ref (APP-12) and a policy project: payload.project, or the key prefix.
A bad line is skipped with one log line, and the line itself is never echoed: it is text from anyone.
A failed run (exit code, timeout) moves no cursor. Event ids deduplicate, so a run can repeat Events.

  command_source.py NAME [--dry-run]    run one Source once, print its Events
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import timedelta

import config
import registry

KEY = re.compile(r"[A-Z][A-Z0-9_]*-\d+")
SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
TEXT = ("what", "actor", "at", "url")


def log(*a):
    print("command_source:", *a, file=sys.stderr)


def check(line, pol, name):
    """Return a clean Event dict, or raise ValueError with a reason. Every field is data from anyone."""
    ev = json.loads(line)
    if not isinstance(ev, dict):
        raise ValueError("not an object")
    for k in ("id", "kind", "ref"):
        if not isinstance(ev.get(k), str) or not ev[k] or len(ev[k]) > 500 or re.search(r"[\x00-\x1f]", ev[k]):
            raise ValueError(f"bad {k}")
    if ev["kind"] not in pol["route"]:
        raise ValueError("kind not in [route]")
    p = ev.get("payload")
    if not isinstance(p, dict):
        raise ValueError("payload is not an object")
    out = {k: re.sub(r"[\x00-\x1f]", " ", p[k])[:300] for k in TEXT if isinstance(p.get(k), str)}
    out.update(self=p.get("self") is True, admitted=p.get("admitted") is True)
    if out["admitted"]:
        if ev["kind"] != "ticket" or not KEY.fullmatch(ev["ref"]):
            raise ValueError("an admitted Event needs kind ticket and a ref like APP-12")
        project = p.get("project") or ev["ref"].split("-")[0]
        if project not in pol["projects"]:
            raise ValueError("project not in the policy")
        out["project"] = project
        if isinstance(p.get("slug"), str) and SLUG.fullmatch(p["slug"]):
            out["slug"] = p["slug"][:60]
    return {"id": f"{name}:{ev['id']}", "kind": ev["kind"], "ref": ev["ref"], "payload": out}


def poll(con, pol, name, dry_run=False):
    """Run the command if its interval passed. Insert its Events. Return the count. dry_run prints, writes nothing."""
    src = pol["sources"][name]
    if not dry_run and not registry.due(con, f"source:{name}:next"):
        return 0
    start = registry.now()
    if not dry_run:
        registry.wait(con, f"source:{name}:next", timedelta(seconds=src["every_seconds"]))  # a failed run also waits
    env = {**os.environ, "TECHICHI_SINCE": registry.cursor(con, f"source:{name}") or ""}
    cmd = [os.path.expanduser(c) for c in src["command"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=src["timeout_seconds"], env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"source {name}: timeout after {src['timeout_seconds']} s") from None
    if r.returncode:
        raise RuntimeError(f"source {name}: exit {r.returncode}")  # stderr can hold data from anyone: not echoed
    n = 0
    for i, line in enumerate(r.stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            ev = check(line, pol, name)
        except (ValueError, json.JSONDecodeError) as e:
            log(f"{name} line {i} skipped: {e}")
            continue
        if dry_run:
            print(json.dumps(ev))
            n += 1
        else:
            n += con.execute("INSERT OR IGNORE INTO events (id, source, kind, ref, payload) VALUES (?, ?, ?, ?, ?)",
                             (ev["id"], name, ev["kind"], ev["ref"], json.dumps(ev["payload"]))).rowcount
    if not dry_run:
        registry.set_cursor(con, f"source:{name}", start)
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="the Source name in [sources.<name>]")
    ap.add_argument("--dry-run", action="store_true", help="print the Events, write nothing")
    a = ap.parse_args()
    pol = config.policy()
    if a.name not in pol["sources"]:
        sys.exit(f"no [sources.{a.name}] in the policy")
    print(poll(registry.connect(), pol, a.name, dry_run=a.dry_run), "events")
