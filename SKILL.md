---
name: "muse-parallel-work"
version: "0.1.0"
license: MIT
description: >
  Manage parallel sub-agent work on a shared machine so batch work never
  starves interactive chat. Covers the principles (the chat agent dispatches
  and stays conversational; fan-out is the default within an admitted budget;
  budgets are derived from measured hardware, not preference), the interim
  concurrency cap (a no-tooling mechanism: build-heavy vs light budgets,
  fan-out counted per child, never kill in-flight work), and the parallelism
  governor — a file-based queue with priority classes P0–P4, admission
  passes, aging, a load circuit breaker, and cron reservations — shipped
  here as a reference implementation (governor.py + tests + design).
  Includes the AGENTS.md template that teaches agents the rules.
when_to_use: >
  Use when setting up or operating multi-agent parallel work on one host:
  "set up the governor", "enqueue this", "what's queued / what's running",
  "the box is overloaded with agents", "tune the limits", "add the
  AGENTS.md parallelism sections", or whenever dispatching sub-agents on a
  machine where interactive chat and batch workers share CPU, RAM, and an
  inference queue.
---

# Muse Parallel Work

How to run many agents on one machine without the machine — or the user's
chat — falling over. Three layers, in the order an adopter meets them:

1. **Principles** — the dispatch rule and the fan-out default. No tooling.
2. **The interim concurrency cap** — two budgets counted by hand. No tooling.
3. **The governor** — a file-based queue that meters admission against
   budgets derived from measured hardware. The reference implementation is
   in this skill: `governor.py`, `test_governor.py`, `limits.yaml`,
   `DESIGN.md`, `REPLAY.md`.

Adopt in order. Each layer keeps working when the next one is added: the
cap stays in force alongside the governor until the governor's activation
gates open, and where they conflict the stricter limit wins.

## Why this exists

On Oct 18, 2026, on a 2 vCPU / 7 GB VM, 14+ sub-agents ran concurrently
across chats — feature implementations compiling Rust, a design task, a
docs refresh, a competitive analysis, and a code-review coordinator that
itself fanned out 11 sub-reviewers — plus scheduled scan classifiers.
Measured load average hit **17.6, about 9x oversubscribed**. The user could
not send messages in some chats; replies took minutes.

No single dispatch was wrong. Every chat agent followed the standing rules
— dispatch heavy work, fan out aggressively — in isolation. Nothing
anywhere counted the total, priced the hardware, or ordered competing
requests against each other. Chat, reminder workers, and batch workers
share the same runtime and inference queue, so batch overload directly
degrades interactivity. This system is the counting, pricing, and ordering.

## Deployment location rule — read before installing anything

**Every mechanism in this system lives entirely under the home directory.**
Governor files, queue state, briefs, toolchain binaries, caches, and config
— all under `~`. Nothing is installed under `/usr`, `/usr/local`, or `/etc`.

On this class of VM those trees are ephemeral and are wiped on replacement.
Proven Oct 19, 2026: a VM event reset `/usr/local` roughly 50 minutes after
a compiler cache (sccache) and a fast linker (mold) were installed there;
every subsequent build invocation failed until both were reinstalled under
`~/bin`. The companion rule for anything you install while adopting this
skill: binaries to `~/bin`, caches to `~/.cache`, config to `~/.config` or
the tool's home-relative config path — never a system tree. The AGENTS.md
template in this skill states the same rule, because the agent that forgets
it will be a future session, not you.

## Principles

### 1. The chat agent dispatches; it does not do the heavy work

The agent in any chat (main or side chat) is a dispatcher and
conversationalist, not the worker, for substantive tasks:

- Coding work — investigation, planning, implementation, debugging,
  release work — is dispatched to a sub-agent, never executed inside the
  chat agent's own turn.
- Deep research and extended web search follow the same pattern.
- On receiving such work, the chat agent replies promptly that the
  instructions were received and dispatched, names what was dispatched in
  one line, then ends its turn and remains available for conversation.
- Results return through the normal handoff; the chat agent reports them
  when they arrive. It does not poll, block, or run long tool chains in
  its own turn while the user waits.
- Quick single-step checks and direct answers (reading one file, a status
  check, a short factual answer) stay in-chat. Dispatch is for work that
  would otherwise occupy the turn.

Why: a chat agent that spends a whole turn executing looks crashed —
messages are accepted but nothing replies until the turn ends.

### 2. Fan-out is the default — inside the admitted budget

Default to wide fan-out with parallel sub-agents on every task where the
work is genuinely independent. Don't wait to be asked, and don't be
conservative about it. "Possible" is the operative word: fan out where
parallelism actually helps, not where it just adds coordination overhead.
Hard constraints still apply — task dependencies (step B needs step A's
output) and API rate limits (more parallel fetchers can make a throttled
API slower, not faster).

**The composition clause:** the chat agent's own turn stays short
*because* the work fans out. Parallelism is something the dispatched
sub-agents do (and a coordinator sub-agent fans out further when a job
needs several workers) — it is never the chat agent running parallel work
inside its own turn. Brief turn, wide fan-out behind it: both rules are
satisfied by the same act of dispatching.

Under the governor, the default is narrowed, not repealed: fan-out happens
*within the admitted budget*. "Parallelize where possible" becomes
"enqueue all the independent pieces; the governor runs as many at once as
the hardware serves well."

### 3. Budgets come from measured hardware

Resource limits are arithmetic, not vibes. Measure the host first: vCPUs,
RAM total and available under load, and what one build of your largest
project actually peaks at. The reference numbers (build 1, light 4, total
5, breaker at load1 5.0) are calibrated to a 2 vCPU / 7 GB VM where one
cargo build fans rustc across both cores and peaks at multiple GB — both
the cores−1 rule and the RAM envelope independently give build = 1 there.
On a bigger host the numbers scale by the same arithmetic (see the tuning
guidance in `limits.yaml`). Never copy the reference numbers onto
unmeasured hardware.

### 4. Chat responsiveness is a hard constraint, not a tradeoff

When load is high, delay the dispatch, never the conversation. Priority
governs admission order only: nothing preempts or kills running work.

## Mechanism 1 — the interim concurrency cap (no tooling)

The pre-governor mechanism, and the standing fallback whenever the
governor's gates are closed:

- At most **4 build-heavy** sub-agents (cargo/npm builds, test gates) run
  concurrently across **all chats combined**. Further build-heavy
  dispatches queue and start as slots free.
- **Light work** (research, docs, reads, analysis) may run up to **8**
  concurrent.
- A coordinator's fan-out counts against the same budgets — the
  coordinator is not a loophole.
- Before dispatching into an already-loaded system, check the live worker
  list and count what is actually running.
- Never kill in-flight jobs to enforce the cap retroactively; the cap
  governs new dispatches.

This cap is crude — it counts workers, not hardware cost, and it has no
queue, no priorities, and no memory across chats. That is what the
governor adds. Keep the cap in force until the governor's activation
gates (below) have both passed on your deployment; the two systems apply
together and the stricter limit wins.

## Mechanism 2 — the governor

A file-based queue that meters every dispatch. Design rationale, the full
decision record, and the council-modifications addendum are in `DESIGN.md`
in this skill; the validation procedure is `REPLAY.md`. This section is
the operating summary.

### Files (all under the governor directory, under home)

| Path | Role |
|---|---|
| `limits.yaml` | Every number. The single place budgets change. |
| `queue.jsonl` | Append-only event log. **Source of truth**; never edited in place. |
| `state.json` | Materialized current state, rewritten atomically (previous copy kept as `state.prev.json`). A cache: rebuildable from `queue.jsonl` at any time via `governor.py rebuild`. |
| `briefs/` | One self-contained brief file per item, written at enqueue. The brief is what makes the queue real: any session can start the item correctly hours later. |
| `metrics.jsonl` | Admission-pass durations; drives the lock-steal threshold. |
| `lock/` | Mutex via atomic `mkdir`, held only for file operations. |

### The operating procedure (every dispatch, every chat)

1. **Write the brief.** A self-contained file under `briefs/` with
   everything a worker needs: task, repo/paths, constraints, gates,
   report-back requirements, preconditions, and a checkpoint/resume
   pointer for long jobs.
2. **Enqueue.**
   `python3 <governor-dir>/governor.py enqueue --title "..." --class P2|P3|P4 --resource build|light --owner-chat-id <id> --brief-file <path>`
   Add `--idempotency-key` for work with a natural key (e.g. a branch
   name) — a duplicate key against a queued/admitted/running item returns
   the existing item instead of creating a second. Add `--fanout N` for a
   coordinator bundle. Add `--depends-on <id>` for ordering.
3. **Snapshot.** Take the current worker list from your platform's
   sub-agent listing and save it as JSON:
   `{"agents":[{"agent_id":..., "status":...}], "cron_active":[...]}`.
   List in `cron_active` the scheduled jobs currently running, by the
   names used in `limits.yaml`'s `cron_reservations`, so their
   light-equivalents are counted.
4. **Admit.** `python3 <governor-dir>/governor.py admit --snapshot <file>`
   — one admission pass under the lock: reconcile the snapshot (finished
   workers close out, absent workers past TTL are interrupted, stale
   reservations reaped), apply cron reservations and the load
   circuit-breaker, promote in priority order skipping items that don't
   fit.
5. **Spawn only the admitted items.** After each spawn record
   `governor.py started --id <id> --worker <agent_id>`.
6. **Close out.** When the handoff lands: `governor.py done --id <id>` or
   `governor.py fail --id <id>`. Closing out is part of the dispatch, not
   bookkeeping to skip — an unclosed item holds its slot forever.
7. **Status any time:** `python3 <governor-dir>/governor.py status`.

**Queue hold:** a file at `<governor-dir>/HOLD` pauses all admissions
while it exists — every admission pass checks for it first. The holder
writes its reason and start time inside the file and removes it when
done. A HOLD older than 3 hours is stale: any chat may remove it after
noting the fact in its reply. (Used, for example, by a weekly
binary-update job that swaps toolchain binaries overnight.)

### Priority classes

| Class | Meaning | Queued? |
|---|---|---|
| **P0** | Interactive chat itself | Never |
| **P1** | Time-critical, due now, user-facing | No — bypasses the queue |
| **P2** | The user is actively waiting on it | Yes, head of queue |
| **P3** | Default batch work | Yes |
| **P4** | Background, can slip | Yes |

- **P1 bypass:** reminders, alerts, due triage, and anything the user
  marks urgent do NOT enter the queue. They run immediately, they pause
  new admissions until they finish, and they never preempt running work.
- **Assignment:** defaults per work type (DESIGN.md §5.3), adjusted by
  the dispatching agent's judgment, overridden by the user's words at any
  time ("urgent" / "I'm blocked" promotes; "no rush" / "background"
  demotes; the user can reclassify a queued item after the fact).
- **Promotion order:** (effective class, base class, enqueue time). The
  base-class tie-break matters: an aged P4 must not precede a fresh P2 at
  equal effective class.
- **Aging:** +1 class per 60 minutes queued, capped at P2. Aging never
  manufactures P1. Nothing waits forever behind fresh P3 work.
- **Fan-out accounting:** a coordinator enqueues a bundle (`--fanout N`):
  a light parent plus N children, **each child consuming its own slot**,
  admitted individually. The parent closes automatically when all
  children are terminal. A coordinator is not a loophole around the
  budgets — per-child counting is the rule whose absence produced 11
  reviewers at once in the incident.

### Budgets, breaker, reservations

- **Build slots: 1** in the reference `limits.yaml` — one worker that
  compiles or runs heavy test gates holds the slot through its build
  *and* its gates (gates are phases of the item, not separate
  admissions).
- **Light slots: 4. Total workers: 5.** The total is the backstop: 5
  light workers with no builder still consume every slot.
- **Circuit breaker:** no admissions while 1-minute load average exceeds
  the threshold (5.0 in the reference limits), checked from
  `/proc/loadavg` on every pass.
- **Cron reservations:** scheduled jobs fire on their own schedule; the
  governor cannot admit, delay, or preempt them. Their footprints are
  declared in `limits.yaml` (reference: hourly email triage 1
  light-equivalent, a multi-classifier scan sweep 3, reminders/watchdog
  0) and counted as occupied budget while active, via the snapshot's
  `cron_active` list.

### Recovery

- A `running` item whose worker is absent from the snapshot past the TTL
  (10 min) is marked **interrupted**. Resumable items auto-requeue at
  their original class (max 2 attempts) per their checkpoint/resume
  markers; others surface to the owner chat.
- An `admitted` item never started within the stale-reservation TTL
  (5 min) returns to `queued` and its budgets are released.
- `state.json` torn or unparsable → rebuild from `queue.jsonl` (damage is
  bounded to a torn final line; `state.prev.json` is the fallback).
- A lock older than the steal threshold — max(5 s floor, 10x the
  measured p95 pass duration) once 5+ passes are measured — is presumed
  abandoned and may be stolen. A waiter whose wait budget expires before
  the threshold fails cleanly with no state change.

### Enforcement is conventional — say so plainly

There is no platform scheduler. The governor works because every chat
agent follows the same written procedure against shared state. An agent
that skips the pass can still oversubscribe the box; reconciliation makes
that visible and attributable after the fact, not impossible. If
convention leaks in practice, the escalation is a real dispatcher
(DESIGN.md §7).

### Known gap: the direct-spawn override

`governor.py started` refuses an item that is queued but not admitted —
there is no override path. On Oct 19, 2026 the user directed a job to
start immediately anyway (spawned directly, outside a pass); the item
was later closed from queued state via `done`, which does accept it.
Until the tool grows an explicit override, the honest handling is: tell
the user the spawn bypassed admission, spawn only on their explicit
instruction, and close the item out normally when it lands.

### Activation gates for the janitor — both CLOSED in the reference deployment

The design's dispatcher is a hybrid: chats admit opportunistically at
dispatch time, and a **janitor cron** (every 15 min) runs admission
passes as a backstop so queued work progresses while nobody is chatting.
The janitor does **not** activate, and the interim cap is **not**
retired, until both gates pass:

1. **The §11 replay** (`REPLAY.md`): replay the incident's load shape
   under the governor and measure — never more than the capped workers,
   load ≤ ~3, a chat probe answered in under 30 s at peak load, every
   item completes, the P4 bundle finishes via aging alone.
2. **The cron-session visibility smoke test:** a one-shot cron confirms
   its worker listing sees the same population the governor reconciles
   against. **Result in the reference deployment (Oct 19, 2026): FAILED
   as a global check.** The cron session's listing returned 0 agents
   while work was verifiably running in chat sessions — a cron session
   sees only its own session. Reconciliation from a janitor that cannot
   see the workers would be unsound, so janitor promotion does not
   activate and **admission stays with chats**, which have evidenced
   global visibility.

A queue-log-only janitor (promotes from the durable log, reconciles by
timeouts alone) is the known alternative if the visibility gate cannot
pass; adopting it is a separate, explicit decision.

## Tuning `limits.yaml`

`limits.yaml` in this skill is a **template** carrying the reference
numbers plus the derivation. The procedure for a new host:

1. Measure: `nproc`, total RAM, RAM available under typical load, and one
   real build's peak RSS and duration for your largest project.
2. Set `build_slots` = cores − 1, then check the RAM envelope: build peak
   + runtime + light workers must fit available RAM with headroom. Take
   the smaller answer.
3. Set `light_slots` from spare RAM (~150–250 MB per light worker) and
   the total: `total_workers` = build + light as the starting point.
4. Set the breaker at ~2.5x vCPUs.
5. Run the test suite (`python3 -m unittest test_governor`), then the
   replay when the host is quiet enough to measure honestly.

## Setting up AGENTS.md

The rules only work if every future session loads them. `AGENTS-TEMPLATE.md`
in this skill is a ready-to-paste AGENTS.md section covering: the dispatch
rule, the parallelism default with the composition clause, the interim
cap, the governor procedure, the queue hold, the home-directory deployment
rule, and setup notes (where the governor files live, how `state.json` and
`queue.jsonl` relate, how to check status, how to adapt the numbers).
Fill in its two placeholders (governor directory, your tuned limits) and
paste it into the AGENTS.md your runtime loads.

## Install

1. Copy this skill folder to your skills directory (reference:
   `~/workspace/custom-skills/muse-parallel-work/`).
2. Create the governor directory **under home** (reference:
   `~/workspace/parallelism-governor/`) and copy into it: `governor.py`,
   `test_governor.py`, `limits.yaml`, `DESIGN.md`, `REPLAY.md`. The
   script creates `queue.jsonl`, `state.json`, `briefs/`, and
   `metrics.jsonl` on first use.
3. Tune `limits.yaml` per the procedure above.
4. Verify: `cd <governor-dir> && python3 -m unittest test_governor`
   (21 tests) and `python3 governor.py status`.
5. Paste the filled-in `AGENTS-TEMPLATE.md` section into your AGENTS.md.
6. Operate under the interim cap + governor procedure together until
   both activation gates pass on your deployment.

## Files in this skill

| File | Role |
|---|---|
| `SKILL.md` | This playbook. |
| `AGENTS-TEMPLATE.md` | The AGENTS.md section to paste into your runtime's instructions. |
| `README.md` | Human-facing summary: what it is, the incident, install, gate status. |
| `governor.py` | The reference implementation (stdlib-only Python 3). |
| `test_governor.py` | 21-test unittest suite for governor.py. |
| `limits.yaml` | Template limits with tuning guidance. |
| `DESIGN.md` | The full design and decision record, incl. the council-modifications addendum. |
| `REPLAY.md` | The §11 validation procedure. |
| `examples/example-brief.md` | A self-contained item brief, as written at enqueue. |
| `VERSION` | Skill version. |
