# The self-contained janitor — full reference

<!-- Moved verbatim from SKILL.md in v0.2.5. SKILL.md keeps the operating summary; this file holds the full rules and their evidence. -->

The governor is a metering layer, not an executor: before the janitor,
admission passes happened only when a chat dispatched or closed out
work. On the night of Oct 20–21, 2026 the queue demonstrated the gap —
queued items sat unadmitted all night even with load near idle, because
no chat ran a pass, and stale running/admitted entries (workers long
finished, never closed out) held slots the whole time. The janitor is
the cron that closes that gap.

## Why it is self-contained: the visibility verdicts

The original design (DESIGN.md §7) had the janitor reconcile against the
platform's worker listing, gated on a smoke test proving a cron session
sees the same population. The test result is definitive:

- **Oct 19, 2026:** a one-shot cron session's worker listing returned
  **0 agents** while other work was verifiably running.
- **Oct 21, 2026, 09:44 EDT:** the direct retest returned 0 agents in
  both direct and descendants scopes while `governor.py status`
  simultaneously reported **5 running/admitted** and 3 queued.
- **Oct 21, 2026, 09:51 EDT:** the spawn variant — a cron session spawned
  exactly one subagent, which ran the listing — also returned **0**: the
  spawned agent could not see other chats' agents, its siblings, itself,
  or even its parent. (The parent cron session could see the child in
  its own list; visibility does not propagate down the lineage.)

Blindness follows the cron session's lineage; spawning does not escape
it. The self-contained design, approved by the owner on Oct 21, 2026,
does not try to fix visibility — it removes the need for it. The janitor
never calls the worker listing and never treats its result as evidence.

## The design

`governor-janitor` runs every **10 minutes** (the DESIGN.md §7 sketch
said 15, and had the janitor hand admitted items to owner chats to
spawn; the as-built janitor spawns its own workers instead — see
"Ownership" below). On every janitor turn:

1. **Close out its own workers first.** If the turn carries a completion
   or failure handoff for a janitor-spawned worker, match it to its
   governor item (by the item's recorded `worker_agent_id`, or the item
   id named in the handoff) and run `governor.py done` / `fail`. A turn
   that is only a handoff close-out logs and ends there.
2. **HOLD check.** On a scheduled tick, if the HOLD file exists, nothing
   else happens this tick; the tick logs the hold and its stated reason
   and ends silently.
3. **Requeue interrupted items** (`governor.py requeue-interrupted`):
   interrupted items return to `queued` below the attempt cap (2) and
   are failed at the cap, so they reach a terminal state their owner can
   see instead of sitting interrupted forever.
4. **Build a state-derived snapshot** (`governor.py janitor-snapshot`):
   the admission snapshot is constructed from governor state plus /proc
   evidence — never from the worker listing. Its diagnostics report the
   live compiler processes, the governor's running build items,
   whether compilers are unexplained (below), and — when the tracker
   entries were read this tick (`--tracker-json`) — which running
   items are checkpoint-protected and which carry malformed
   checkpoints (`checkpoint_malformed`).
5. **Run one admission pass** (`governor.py admit --snapshot <file>`,
   with `--block-build` when the guard trips). Slot limits and the load
   circuit breaker come from `limits.yaml`; while load1 is above the
   breaker the pass admits nothing, but close-outs and reconciliation
   still run.
6. **Spawn at most 2 admitted items per tick**, in the order the pass
   returned them: read each item's brief file, spawn one subagent with
   the brief's full contents verbatim, and record
   `governor.py started --id <item> --worker <agent_id>`. An item whose
   brief file is missing is never spawned (logged as
   admitted-but-unspawnable; it does not consume the spawn cap). An item
   already admitted or running is never spawned again. The janitor does
   not wait for its workers; their results return as later janitor
   turns.
7. **Log every tick** — exactly one line in the janitor log
   (`hidden_files/janitor-log.md`): timestamp, closed items,
   requeued items, reaped/reconciled items with their evidence,
   `checkpoint-malformed <id>` flags, admitted and spawned items, and
   the rails state (HOLD, load1, breaker, unexplained compilers).

## Reconciliation and the stale-entry rules

Reconciliation runs from the queue log/state plus /proc, under hard
safety rails. Every uncertainty resolves to **admit less / reap
nothing**.

- **Admitted, never started:** an admitted item older than the stale
  window (`admitted_stale_minutes` in `limits.yaml`; 5 in the template)
  with no `started` record returns to `queued` in the admission pass and
  its budgets are released.
- **Running items** are protected — reported as running in the snapshot,
  and therefore left alone — when any of these hold:
  - the item started **less than 60 minutes ago** (the as-built helper
    applies this blanket to every resource; the brief's hard rule named
    light items, and the implementation is the conservative superset);
  - it is a **build item and any compiler process is alive** anywhere on
    the VM — a build item is **never** reaped while any `cargo`,
    `rustc`, or `clippy-driver` process lives, because /proc evidence
    cannot attribute a compiler process to one item, so every build is
    protected;
  - explicit liveness evidence for its worker id is supplied;
  - its linked Work Tracker entry carries a **checkpoint younger than
    10 minutes** — see "Checkpoint heartbeats" below.
- An unprotected running worker is omitted from the snapshot, and the
  ordinary reconcile rule decides: absent past the running-absent TTL
  (`running_absent_minutes`; 10 in the template) the item is marked
  `interrupted`, and the requeue step returns it to `queued` under the
  attempt cap. Chat-owned entries are reconciled by exactly the same
  rules — that is what fixes the phantom-entry problem (the three stale
  entries that held slots for ~16 hours on Oct 20–21, 2026).
- Every reap is logged with its evidence. Ambiguous evidence leaves the
  entry and the item alone. And reaping is an accounting action, never a
  kill: it frees the governor's slot accounting and requeues the item;
  a worker that was actually still alive keeps running, and its result
  still returns to its owner.

## Checkpoint heartbeats

Age and compiler evidence cannot distinguish a worker in a long
non-compiling phase from a dead one — in the reference deployment, two
genuinely running builds were reaped on Oct 21, 2026 after crossing 60
minutes with no compiler process alive, and two other items burned
their attempt cap through interrupted cycles. The fix is a
worker-maintained heartbeat in the Work Tracker (Mechanism 3):

- **The link.** A governor item may carry `tracker_ref` — the id of its
  Work Tracker entry — recorded with `enqueue --tracker-ref <id>`, at
  spawn time with `started --tracker-ref <id>`, or retrofitted onto an
  existing queued, admitted, or running item with
  `set-tracker-ref --id <item> --tracker-ref <id>` (v0.2.2; terminal
  items are refused, since a closed item's checkpoints could never be
  read). Items without a link get no checkpoint protection.
- **The write.** While the item runs, its worker updates the entry's
  `details` (the tracker's update action) at least every 5 minutes:
  generate the heartbeat line with
  `governor.py checkpoint-line --id <item> --state "<one-line state>"`,
  remove any previous checkpoint line, preserve the rest of the text,
  and append the printed line verbatim as the last line. The command
  derives the item id from the item's own record and the timestamp
  from the system clock, and refuses unknown, unlinked, and terminal
  items. **Workers must never hand-write the checkpoint line, an item
  id, or a timestamp** (v0.2.3): the first real checkpoints were
  hand-written and failed three ways — the original item's id instead
  of the running item's, the marker buried mid-paragraph where a
  line-start parser could not see it, and a timestamp computed
  exactly one year stale — each silently voiding protection. The line
  format the command prints:
  `GOVERNOR-CHECKPOINT {"item": "<governor item id>", "ts": <unix epoch seconds>, "state": "<one-line state>"}`
- **The read.** Each janitor tick, when any running item is
  tracker-linked, the janitor reads the tracker entries back (the list
  action — an ordinary agent tool, available in cron lineage, unlike
  the worker listing), extracts the heartbeats with
  `extract-checkpoints`, and passes them to the snapshot
  (`janitor-snapshot --checkpoints <file> --tracker-json <listitems file>`).
  A checkpoint younger than 10 minutes (with a 60-second tolerance for
  future clock skew) protects the item exactly like the <60-minute
  blanket, past every age/compiler evidence limit. The parser finds
  the marker anywhere in the entry's text, not only at line start;
  the last occurrence whose JSON parses wins.
- **Binding rules.** A checkpoint carried by any entry other than the
  item's linked entry confers nothing; a stale (>10 min) or missing
  checkpoint confers nothing; and because the snapshot only ever lists
  running items, a checkpoint can never protect a terminal item or
  block a close-out.
- **Malformed checkpoints are visible, not silent.** A running,
  tracker-linked item whose linked entry *contains* the marker but
  yields no valid bound checkpoint — unparseable JSON, a checkpoint
  naming a different item, or a timestamp outside the freshness
  window — is reported in the snapshot diagnostics as
  `checkpoint_malformed` and logged by the janitor as
  `checkpoint-malformed <id>`. An entry with no marker at all is not
  flagged: absence is the ordinary stale case, not malformation.
- **The convention is part of the contract.** The AGENTS.md template
  carries the worker-side rule, and briefs for long-running
  tracker-linked items repeat it. A worker that never checkpoints is
  indistinguishable from a dead one once the evidence windows close —
  by design.

## Conservative admission

- **State-derived snapshot only.** Usage is counted from the governor's
  own admitted/running entries after reconciliation. The snapshot's
  `cron_active` list is always empty in a janitor pass: the janitor does
  not claim scheduled-job reservations, and is bounded instead by
  state-derived usage, the load breaker, and the per-tick spawn cap.
  (Chat-run passes still list active crons in their snapshots; see
  Mechanism 2.)
- **Unexplained-compiler guard.** The snapshot scans /proc for `cargo` /
  `rustc` / `clippy-driver` processes. If compilers are alive and **no**
  governor running build item exists, they are unexplained: build
  capacity is treated as fully occupied for that pass (`--block-build`)
  and only light items may be admitted. The check is at item-class
  level — the helper does not attribute individual processes to items —
  so treat it as a coarse guard, not proof that every live compiler
  belongs to the governor.
- **HOLD file:** present means the scheduled tick does nothing at all
  (after any handoff close-out and the log line).
- **Load breaker:** load1 above the `limits.yaml` threshold admits
  nothing that pass.
- **Spawn cap:** at most **2 spawns per tick**, however many items the
  pass admits; extra admitted items stay admitted for later passes to
  reconcile.
- **Never** spawn an item whose brief is missing, an item already
  admitted/running, or any P1 work — P1 never enters this system.

## Ownership and supervised mode

The janitor **owns the workers it spawns end-to-end**: their handoffs
return to the janitor's lineage, and closing them out in the governor is
the janitor's first act on every subsequent turn. Workers still route
their final reports per their briefs (the item's owner chat is recorded
on the item); the janitor's close-out is what the governor records.

Reporting is **supervised mode** until the owner changes it: every tick
appends its log line; a tick that took any action (close, requeue, fail,
reap, admit, spawn) also reports briefly to the main chat; a no-action
tick stays silent.

The janitor never enqueues new work, never edits `queue.jsonl` or
`state.json` by hand, and never changes `limits.yaml`.

## Implementation prerequisite

The janitor depends on four governor commands — `janitor-snapshot`
(with `--checkpoints` and `--tracker-json`), `admit --block-build`,
`requeue-interrupted`, and `extract-checkpoints` — plus the
`tracker_ref` item field and the worker-side `checkpoint-line` and
`set-tracker-ref` commands. The `governor.py` shipped in this skill
(v0.2.5) includes all of them, with unit tests (snapshot protection
rules including checkpoints, checkpoint-line generation and refusals,
mid-paragraph marker parsing, malformed-checkpoint flagging, the
block-build pass, the /proc compiler scan, interrupted requeue under
the attempt cap, and an end-to-end phantom-reaping flow). If you port
the governor instead of adopting the shipped file, port that behavior,
with tests, before enabling the janitor cron from
`templates/janitor-cron-body.md` — the checklist
(`templates/install-checklist.md`) makes it a gate.
