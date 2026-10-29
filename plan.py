#!/usr/bin/env python3
"""The planner's tool, and the only writer of the plan table. See DESIGN.md section "Planner"."""
import argparse
import json
import os
import sys

import config
import herdr
import registry

NOW = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"


def by():
    return os.environ.get("HERDR_PANE_ID") or "cli"


def show(con, pol):
    cap = pol["spawn"]["max_active"]
    print(f"Spawned workers: {registry.slots_used(con)} active of {cap}.")
    waiting = con.execute("SELECT e.ref, e.reason, e.created_at, e.payload, p.position, p.hold FROM events e "
                          "LEFT JOIN plan p ON p.ticket = e.ref WHERE e.kind = 'ticket' AND e.status = 'queued' "
                          "AND json_extract(e.payload, '$.admitted') ORDER BY p.position IS NULL, p.position, e.created_at"
                          ).fetchall()
    print(f"\nAdmitted tickets that wait: {len(waiting)}")
    for w in waiting:
        print(f"  {w['ref']:<10} {w['reason'] or 'queued':<11} {json.loads(w['payload']).get('url', '')}")
    rows = con.execute("SELECT * FROM plan ORDER BY position IS NULL, position, ticket").fetchall()
    print(f"\nPlan rows: {len(rows)}")
    for r in rows:
        state = "hold" if r["hold"] else "ready"
        print(f"  {r['position'] if r['position'] is not None else '-':>3} {r['ticket']:<10} {state:<5} "
              f"{r['note'] or ''}  ({r['updated_by']}, {r['updated_at']})")


def order(con, keys):
    keys = [k.upper() for k in keys]
    if len(set(keys)) != len(keys):
        sys.exit("order: a key appears twice")
    con.execute("BEGIN IMMEDIATE")
    for i, k in enumerate(keys, 1):
        con.execute(f"INSERT INTO plan (ticket, position, updated_by, updated_at) VALUES (?, ?, ?, {NOW}) "
                    "ON CONFLICT (ticket) DO UPDATE SET position = excluded.position, "
                    "updated_by = excluded.updated_by, updated_at = excluded.updated_at", (k, i, by()))
    con.execute("COMMIT")


def hold(con, key, note, on):
    con.execute(f"INSERT INTO plan (ticket, hold, note, updated_by, updated_at) VALUES (?, ?, ?, ?, {NOW}) "
                "ON CONFLICT (ticket) DO UPDATE SET hold = excluded.hold, note = coalesce(excluded.note, note), "
                "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (key.upper(), int(on), note, by()))


def claim_role(con):
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        sys.exit("claim-role: HERDR_PANE_ID is not set. Run it inside a herdr pane.")
    try:
        sid = herdr.session_id(herdr.agent(pane))
    except (herdr.HerdrError, OSError, ValueError):
        sid = None
    if not sid:
        sys.exit(f"claim-role: herdr shows no Claude session in pane {pane}")
    registry.upsert_session(con, sid, pane_id=pane)
    con.execute("UPDATE sessions SET role = 'planner' WHERE session_id = ?", (sid,))
    registry.link(con, "inbox", config.policy()["factory"]["inbox_ref"], sid, "claim-role")  # DMs and mentions go to the planner
    print(f"Session {sid} in pane {pane} is the planner.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="waiting admitted tickets, plan rows, spawn capacity")
    o = sub.add_parser("order", help="set positions 1 to n in this order")
    o.add_argument("keys", nargs="+")
    h = sub.add_parser("hold", help="keep a ticket from spawning")
    h.add_argument("key")
    h.add_argument("--note", required=True)
    r = sub.add_parser("release", help="end a hold")
    r.add_argument("key")
    sub.add_parser("claim-role", help="make this session the planner")
    a = ap.parse_args(argv)
    con = registry.connect()
    if a.cmd == "show":
        show(con, config.policy())
    elif a.cmd == "order":
        order(con, a.keys)
    elif a.cmd == "hold":
        hold(con, a.key, a.note, True)
    elif a.cmd == "release":
        hold(con, a.key, None, False)
    elif a.cmd == "claim-role":
        claim_role(con)


if __name__ == "__main__":
    main()
