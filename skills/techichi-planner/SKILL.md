---
name: techichi-planner
description: Decide which admitted Jira tickets the factory starts, and in which order, when tickets compete for slots or need a decision. Use when a "[factory]" prompt says that tickets need a plan or a decision, when the factory starts this session as the planner, or when the operator asks to plan the sprint for the factory. It reads the sprint in Jira, asks the operator where the team set the order, finds conflicts, drafts an order with holds, and writes it with plan.py after the operator approves. NOT for work on a ticket. A worker session uses the factory worker skill for that.
---

# Factory planner

BLUF: Draft the start order for the sprint tickets. Show the draft to the operator. Write it with `plan.py` only after the operator approves.

The factory starts one worker session for each admitted ticket. Usually it needs no plan. The factory opens the planner only in these cases:

- `needs-plan`: more tickets wait than the free slots. No ticket starts in Jira order, because that order does not match what the team wants first. Only planned tickets start, lowest position first.
- `needs-decision`: the spawner found existing work for the ticket, a branch or an open pull request. The prompt names it.
- `spawn failed`: the spawner failed for the ticket. Tell the operator what `plan.py show` and the daemon log say. The factory does not retry a failed spawn. The operator runs `spawn.py` by hand.

The factory starts this session itself, or sends it a prompt that lists the tickets and their reasons. The design is in `{factory}/DESIGN.md`.

## Rules

- Never start a worker. Never run `spawn.py`.
- Never edit `policy.toml`.
- Never write to Jira or Slack. Read only.
- Write the plan only with `plan.py`. Never edit `registry.db` by hand.
- To see the whole factory, run `python3 {factory}/factory.py status`. It reads only.
- When the operator approves it, link a session that the operator started by hand to its ticket, PR, or Slack thread: `python3 {factory}/factory.py claim REF --session PANE`. Never run `factory.py cleanup`.

## Procedure

1. Run `python3 {factory}/plan.py claim-role`. This session becomes the planner.
2. Run `python3 {factory}/plan.py show`. Note the tickets that wait, their reasons, the plan rows, and the free slots.
3. Read the sprint with the Atlassian MCP tools. Get the operator's tickets in the policy projects in the open sprint and in the next sprint. For each ticket, read the priority, the status, the issue links, and the description.
4. Ask the operator where the team set the order for this sprint. Read that source.
5. Find the conflicts:
   - Two tickets that change the same code area. Start one, and hold the other until the first PR merges.
   - A ticket that depends on another ticket. The dependency goes first.
   - A ticket that a session already works on. Check `factory.py status`, `herdr agent list`, and `plan.py show`. Ask the operator to claim the ticket for that session, or hold the ticket with a note that names the session.
   - A ticket that waits for a product decision. Hold it with a note that names the open question.
6. For each `needs-decision` ticket, show the operator the branch or the pull request that the prompt names. Ask the operator to choose one:
   - Hold the ticket, for example because a hand-started session owns that work.
   - Approve the start. Then the ticket goes into `plan.py order`. A plan row tells the spawner that the operator approved, so the pre-check passes.
7. Write the draft for the operator:
   - The order, one line for each ticket, with one reason.
   - The holds, one line for each ticket, with the note.
   - The free slots, and which tickets start first.
8. Wait for the operator to approve or change the draft. Do not write before the approval.
9. Run `python3 {factory}/plan.py order KEY KEY ...` with the approved order.
10. For each approved hold, run `python3 {factory}/plan.py hold KEY --note "TEXT"`.
11. Run `plan.py show` again. Show the result to the operator in three lines or fewer.

## Later changes

- To end a hold, run `python3 {factory}/plan.py release KEY`.
- To change the order, run `plan.py order` again with the new order. Rows for other keys stay.
- A plan row can exist before its ticket is admitted. Thus the operator can plan a sprint before it starts.
