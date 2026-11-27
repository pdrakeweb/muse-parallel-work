---
name: "muse-parallel-work"
version: "0.2.5"
license: MIT
description: >
  Meter parallel sub-agent work on one shared host so batch work never
  starves interactive chat: the dispatch rule, the interim concurrency cap,
  and a file-based parallelism governor (governor.py: P2-P4 queue, admission
  passes against build/light/total budgets, aging, load breaker, HOLD file)
  plus the Work Tracker refill cron, the self-contained janitor cron, and
  worker checkpoint heartbeats, and brief_lint.py, which rejects artifact briefs missing the standing instruction. Use when dispatching or fanning out
  sub-agents on a machine chat also runs on, when enqueueing, admitting,
  closing out, or checking governor items, when the box is overloaded or
  the queue is stuck, when tuning limits.yaml, or when installing or
  operating the refill or janitor crons. Not for single-agent tasks, quick
  in-chat answers, or P1 work (reminders, alerts, urgent items), which
  bypass the queue.
when_to_use: >
  "set up the governor", "enqueue this", "what's queued / running", "the
  box is overloaded with agents", "tune the limits", "add the AGENTS.md
  parallelism sections", "set up the janitor", "why is the queue not
  moving", "scan the work tracker", "refill the queue", "what crons touch
  admission", or any sub-agent dispatch on a host where chat and batch
  workers share CPU, RAM, and an inference queue.
---

Repository: https://github.com/pdrakeweb/muse-parallel-work


# Muse Parallel Work

Run many agents on one machine without the machine, or the user's chat,
falling over. Five layers, adopted in order (`templates/install-checklist.md`);
each layer keeps working when the next is added, and where two conflict the
stricter limit wins.

1. **Principles**: dispatch, fan out within budget. No tooling.
2. **Interim cap**: two hand-counted budgets. No tooling.
3. **Governor**: `governor.py` meters admission against measured budgets.
4. **Backlog**: a Work Tracker plus an hourly fill-only refill cron.
5. **Janitor and control crons**: a self-contained janitor that reconciles
   and admits on a tick, plus the HOLD protocol.

Why it exists and the evidence behind each rule: `references/history.md`.

## Hard rules (always apply)

- **Home directory only.** Every governor file, toolchain binary, cache, and
  config lives under `~` (binaries `~/bin`, caches `~/.cache`, config
  `~/.config`). Never `/usr`, `/usr/local`, or `/etc`: VM replacement wipes them.
- **Chat dispatches; it does not do heavy work.** Coding (investigation,
  planning, implementation, debugging, release), deep research, and
  extended web search go to a sub-agent. Reply promptly that the work was
  received and dispatched, name it in one line, end the turn, and report
  results when the handoff arrives. Never poll, block, or run long tool
  chains while the user waits. One-step checks and direct answers stay in-chat.
- **Fan out by default, inside the admitted budget.** Parallelize genuinely
  independent work without being asked; respect task dependencies and API
  rate limits. Fan-out is done by dispatched sub-agents (a coordinator for
  multi-worker jobs), never inside the chat agent's own turn. Under the
  governor: enqueue every independent piece and let admission set the width.
- **Budgets come from measured hardware**, never preference (see Tuning).
- **Chat responsiveness is a hard constraint.** Under load, delay the
  dispatch, never the conversation. Priority orders admission only;
  nothing preempts or kills running work.
- **Enforcement is conventional.** No platform scheduler exists; the system
  works only because every agent follows this procedure against shared
  state. Skipping it can still oversubscribe the box. Reconciliation makes
  that visible after the fact. It does not make it impossible.

## Interim concurrency cap (always in force for chat dispatch)

- At most **4 build-heavy** sub-agents (cargo/npm builds, test gates) across
  **all chats combined**; further ones queue until a slot frees.
- **Light work** (research, docs, reads, analysis): up to **8** concurrent.
- A coordinator's fan-out counts against the same budgets.
- Check the live worker list before dispatching into a loaded system.
- Never kill in-flight jobs to enforce the cap; it governs new dispatches.
- Retiring the cap is an explicit owner decision, never a side effect of
  installing the governor or janitor. The reference deployment has not retired it.

## Governor procedure (every dispatch, every chat)

`G=python3 <governor-dir>/governor.py`. Files live in the governor directory:
`queue.jsonl` (append-only event log, the source of truth, never hand-edited),
`state.json` (atomic cache, rebuildable with `$G rebuild`; previous copy in
`state.prev.json`), `briefs/`, `metrics.jsonl`, `limits.yaml` (every number),
`lock/`, and optional `HOLD`.

1. **Write the brief**: one self-contained file under `briefs/` (task,
   repo/paths, constraints, gates, report-back, preconditions, resume pointer
   for long jobs). See `examples/example-brief.md`.
2. **Enqueue**: `$G enqueue --title "..." --class P2|P3|P4 --resource build|light --owner-chat-id <id> --brief-file <path>`.
   Optional flags: `--idempotency-key <k>` (a key matching a live item returns
   that item instead of a duplicate), `--fanout N` (coordinator bundle),
   `--depends-on <id>` (repeatable), `--tracker-ref <work-tracker-id>`
   (enables checkpoint protection).
3. **Snapshot**: save the platform's sub-agent listing as
   `{"agents":[{"agent_id":..., "status":...}], "cron_active":[...]}`, listing
   running scheduled jobs by their `limits.yaml` `cron_reservations` names.
4. **Admit**: `$G admit --snapshot <file>`. One pass under the lock:
   reconcile, auto-close finished bundles, then, unless HOLD exists or the
   load breaker trips, promote in priority order, skipping items that do not
   fit. The JSON result reports `admitted`, `interrupted`, `requeued`,
   `circuit_breaker`, and `hold`.
5. **Spawn only admitted items**, then `$G started --id <id> --worker <agent_id> [--tracker-ref <id>]`.
6. **Close out** when the handoff lands: `$G done --id <id>` or
   `$G fail --id <id> --error "<cause>"`. An unclosed item holds its slot
   until reconciliation reaps it, so closing out is part of the dispatch.
7. **Status** any time: `$G status`.

**Direct-spawn override (known gap):** `started` refuses a queued,
unadmitted item, and no override exists. If the user orders an immediate
start anyway, spawn only on that explicit instruction, tell them it
bypassed admission, and close the item normally (`done` accepts a queued item).

### Brief lint — the artifact standing rule, enforced

`brief_lint.py` (stdlib-only) checks a brief before enqueue: a brief whose
output becomes an artifact, dashboard, UI, widget, wishlist, page, tracker,
or infographic must carry the user's standing artifact instruction verbatim.
`brief_lint.py --fix <brief>` appends it. Classification: `--artifact` /
`--no-artifact` flags win; otherwise frontmatter `output_type` / `output`
against the artifact type list; otherwise a keyword heuristic over the body
that errs toward in scope.

`governor.py` enqueue runs the lint first: an in-scope brief missing the
line is rejected before any queue write (fix with `brief_lint.py --fix
<brief>` and re-enqueue). Never bypass a rejection — fix the brief.
Tests: `test_brief_lint.py` (`python3 -m unittest test_brief_lint`).

### Priority classes

| Class | Meaning | Queued? |
|---|---|---|
| P0 | Interactive chat itself | Never |
| P1 | Time-critical, due now, user-facing (reminders, alerts, due triage, anything marked urgent) | No: runs at once, pauses new admissions until done, never preempts |
| P2 | User is actively waiting | Yes, head of queue |
| P3 | Default batch work | Yes |
| P4 | Background, can slip | Yes |

- Assign by work type (DESIGN.md §5.3) and judgment. The user's words
  override at any time: "urgent" or "I'm blocked" promotes, "no rush" or
  "background" demotes, and queued items can be reclassified.
- Promotion order is (effective class, base class, enqueue time), so an aged
  P4 never precedes a fresh P2. Aging gives +1 class per 60 min queued,
  capped at P2. It never produces P1.
- `--fanout N` makes a light parent plus N children, **each child consuming
  its own slot**. The parent closes when every child is terminal.

### Budgets, breaker, reservations (reference `limits.yaml`)

- **Build 1** (a build item holds its slot through its build *and* gates),
  **light 4**, **total 5** (the backstop).
- **Load breaker:** no admissions while `/proc/loadavg` load1 exceeds the
  threshold (5.0 reference). Reconciliation still runs.
- **Cron reservations:** scheduled jobs cannot be admitted, delayed, or
  preempted. Their footprints in `cron_reservations` (reference: email triage
  1, scan sweep 3, reminders 0) count as occupied while listed in `cron_active`.

### Recovery

- Running item absent from the snapshot for more than 10 min
  (`running_absent_minutes`): marked **interrupted**. `$G requeue-interrupted`
  requeues it at its original class below 2 attempts, otherwise fails it,
  so it reaches a terminal state the owner can see.
- Admitted but not started within 5 min (`admitted_stale_minutes`): returns
  to queued and releases its budget.
- `state.json` unreadable: rebuild from `queue.jsonl`. A torn log line costs
  only that line.
- A lock older than max(5 s, 10 x measured p95 pass time; 5+ samples) is
  presumed abandoned and stolen. A waiter whose wait expires first fails
  cleanly (`lock_busy`, exit 3) with no state change. Retry once after ~15 s.

## HOLD protocol

- `<governor-dir>/HOLD` pauses admissions while it exists. `admit` enforces
  it (`"hold": true`, nothing admitted, reconciliation still runs), and
  `status` shows it. Chats and the janitor also check it before a pass.
- Writers: the owner, a chat acting on the owner's instruction, or a
  maintenance job (the weekly binary update). The holder writes its reason
  and an ISO start time as the content.
- Remove only your own HOLD. A job that finds someone else's HOLD under 3
  hours old aborts; it never overwrites it.
- A HOLD older than **3 hours** is stale: any chat may remove it after noting
  that in its reply. The weekly job notes a stale HOLD, removes it, then
  writes its own.

## Worker checkpoints (tracker-linked items)

If your item has a `tracker_ref`, update that Work Tracker entry's `details`
at least **every 5 minutes**. Generate the line with
`$G checkpoint-line --id <item> --state "<one-line state>"`, remove any previous
checkpoint line, keep the rest of the text, and append the printed line
verbatim as the last line. **Never hand-write the line, an item id, or a
timestamp.** The command derives both and refuses unknown, unlinked, and
terminal items. Link an item with `enqueue --tracker-ref`,
`started --tracker-ref`, or `set-tracker-ref --id <item> --tracker-ref <id>`
(live items only). A checkpoint younger than 10 min protects a running item
like a live sighting. Without one, a long non-compiling phase past 60 min
looks dead and is reaped. Full binding and malformed-checkpoint rules:
`references/janitor.md`.

## Backlog and refill (summary)

The governor queue coordinates ready work. The **Work Tracker** (projects
General/LLMdil; statuses Requested, Pending, In Progress, Blocked, Done,
Future) is the backlog of record. Read it only through its actions, never
its database file. The hourly `governor-queue-refill` cron **fills only**: it
never admits, never spawns, and never edits a tracker. It runs only when the
system is idle (no HOLD; 0 running, 0 admitted, 0 queued; load1 under the
breaker as shown by `status`). It classifies candidates as EXECUTABLE,
NEEDS USER (any decision, spend, booking, send, delete, or account change),
or ILL-DEFINED (sent to council, at most 2 per run, with a 7-day cooldown;
enqueued at confidence of MEDIUM or higher). It enqueues at most 5 per run at
P3 (P2 if marked urgent or due within 48 h), using the stable key
`refill-<source>-<id>` and `--tracker-ref` for Work Tracker items. It reports
only when it did something. Full rules: `references/backlog-and-refill.md`.
Cron body: `templates/refill-cron-body.md`.

## Janitor (summary)

`governor-janitor` runs every **10 min** and owns the workers it spawns
end-to-end. It never reads the worker listing, because cron-lineage
sessions see zero agents. Each tick runs these steps in order:

1. Close out its own finished or failed workers.
2. Check HOLD. If present, log the tick and stop.
3. Run `requeue-interrupted`.
4. Build the snapshot from state and /proc with `janitor-snapshot`. Add
   `--checkpoints` and `--tracker-json` when it read the tracker this tick.
5. Run one `admit`. Add `--block-build` when compilers are alive and no
   running build item explains them.
6. Spawn **at most 2** admitted items, each with its brief verbatim, and
   record each with `started`. Never spawn a missing-brief, already-running,
   or P1 item.
7. Write exactly one log line.

Every uncertainty resolves to admitting less and reaping nothing. A reap is
an accounting action, never a kill. The janitor never enqueues, never edits
`queue.jsonl`/`state.json`, and never changes `limits.yaml`. It reports to
the main chat only on ticks that took action (supervised mode). Full
design: `references/janitor.md`. Cron body: `templates/janitor-cron-body.md`.

## Brief lint

`enqueue` runs `brief_lint.py` (it must sit beside `governor.py`) before any
queue write. A brief whose output becomes an artifact, dashboard, UI,
widget, wishlist, page, tracker, or infographic must carry the user's
standing artifact instruction word for word. A brief that lacks it is
rejected with exit code 4 and `{"error": "brief_lint"}`. Fix it with
`python3 brief_lint.py --fix <brief>` and enqueue again. Never get around a
rejection by rewording the brief to dodge the classifier. Classification:
`--artifact`/`--no-artifact` on brief_lint, then frontmatter
`output_type`/`output`, then a body keyword heuristic that errs toward in
scope.

## Control cron inventory

| Cron | Cadence | Role | Boundary |
|---|---|---|---|
| `governor-janitor` | 10 min | Admit, spawn (max 2), reconcile, close own workers | Never enqueues or reads the worker listing; HOLD stops it |
| `governor-queue-refill` | Hourly | Fill queue from trackers when idle | Never admits, spawns, or edits a tracker |
| `weekly-binary-updates` | Sun ~03:00 | Toolchain maintenance under a HOLD | Waits for a compiler-quiet window; removes only its own HOLD |

Reservation-consuming jobs (email triage, scans, reminders) are not control
crons and never touch the queue.

## Tuning `limits.yaml`

1. Measure `nproc`, total RAM, RAM available under load, and one real
   build's peak RSS for your largest project.
2. `build_slots` = cores - 1, capped by the RAM envelope (build peak +
   runtime + light workers fit with headroom). Take the smaller value.
3. `light_slots` from spare RAM (~150-250 MB each). `total_workers` = build
   + light to start.
4. Breaker at ~2.5 x vCPUs.
5. Run `python3 -m unittest test_governor test_brief_lint` (81 + 9 tests). `REPLAY.md` is
   optional validation, not a gate.

## Install and files

Follow `templates/install-checklist.md`: copy the skill, create the
governor directory under home with `governor.py`, `brief_lint.py`, `test_governor.py`,
`test_brief_lint.py`, `limits.yaml`, `DESIGN.md`, and `REPLAY.md`, tune, verify, paste the
filled-in `AGENTS-TEMPLATE.md` into AGENTS.md, then add the tracker, the
refill cron, and the janitor cron, in that order.

| File | Role |
|---|---|
| `AGENTS-TEMPLATE.md` | AGENTS.md section future sessions load |
| `governor.py` / `test_governor.py` | Reference implementation (stdlib Python 3) and 81-test suite |
| `brief_lint.py` / `test_brief_lint.py` | Brief lint run by `enqueue`; `--fix` appends the standing artifact instruction |
| `limits.yaml` | Template limits with derivations |
| `DESIGN.md` / `REPLAY.md` | Design and decision record; optional load-replay validation |
| `references/janitor.md` | Janitor design, reconciliation, checkpoints, conservative admission |
| `references/backlog-and-refill.md` | Work Tracker schema and full refill rules |
| `references/history.md` | Incident, evidence, and gate-status record |
| `templates/` | Janitor and refill cron bodies, install checklist |
| `examples/example-brief.md` | A self-contained brief |
| `CHANGELOG.md` / `VERSION` | Version history |
