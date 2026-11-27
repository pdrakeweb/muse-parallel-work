---
name: "muse-parallel-work"
version: "0.2.4"
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
  Version 0.2.0 covers the operating system around the governor: a Work
  Tracker backlog of record, an hourly fill-only refill cron that
  classifies tracker work and enqueues it under stable idempotency keys,
  a self-contained janitor cron that reconciles from queue state and
  /proc evidence instead of worker-list visibility, and the
  admission/control cron inventory, including the HOLD protocol.
  Version 0.2.1 adds worker checkpoint heartbeats: a worker on a
  tracker-linked governor item writes a GOVERNOR-CHECKPOINT line into
  its Work Tracker entry at least every 5 minutes, and the janitor
  honors a checkpoint younger than 10 minutes as liveness evidence
  equal to a live worker sighting — so a run in a long non-compiling
  phase is no longer indistinguishable from a dead one. The shipped
  governor.py and test suite are updated to match (46 tests).
  Version 0.2.2 adds `set-tracker-ref`, a retrofit command that links
  an existing queued, admitted, or running item to its Work Tracker
  entry after the fact (terminal items are refused), so an item
  enqueued before its tracker entry existed — or whose worker created
  the entry at spawn — can still gain checkpoint protection. The
  shipped test suite grows to 55 tests.
  Version 0.2.3 hardens the checkpoint convention after the first
  real checkpoints were hand-written wrong three ways (wrong item id,
  marker buried mid-paragraph, timestamp one year stale — each
  silently voiding protection): a new `checkpoint-line` command
  generates the fully-formed heartbeat line with the id and timestamp
  machine-derived, so workers never hand-write the line, an item id,
  or a timestamp; the checkpoint parser now finds the marker anywhere
  in the entry text, not only at line start; and the janitor flags
  malformed checkpoints (`checkpoint_malformed` in the snapshot
  diagnostics, `checkpoint-malformed <id>` in its log) instead of
  letting them fail silently. The shipped test suite grows to
  72 tests.
  Version 0.2.4 adds `brief_lint.py`: enqueue rejects an in-scope
  brief missing Pete's standing artifact instruction, before any queue
  write; `--fix` appends the verbatim line.
  Includes the AGENTS.md template that teaches agents the rules, plus a
  templates pack (janitor cron body, refill cron body, install
  checklist).
when_to_use: >
  Use when setting up or operating multi-agent parallel work on one host:
  "set up the governor", "enqueue this", "what's queued / what's running",
  "the box is overloaded with agents", "tune the limits", "add the
  AGENTS.md parallelism sections", "set up the janitor", "why is the
  queue not moving", "scan the work tracker", "refill the queue", "what
  crons touch admission", or whenever dispatching sub-agents on a
  machine where interactive chat and batch workers share CPU, RAM, and an
  inference queue.
---

Repository: https://github.com/pdrakeweb/muse-parallel-work


# Muse Parallel Work

How to run many agents on one machine without the machine — or the user's
chat — falling over. Five layers, in the order an adopter meets them:

1. **Principles** — the dispatch rule and the fan-out default. No tooling.
2. **The interim concurrency cap** — two budgets counted by hand. No tooling.
3. **The governor** — a file-based queue that meters admission against
   budgets derived from measured hardware. The reference implementation is
   in this skill: `governor.py`, `test_governor.py`, `limits.yaml`,
   `DESIGN.md`, `REPLAY.md`.
4. **The backlog** — a Work Tracker that holds pending work as the record,
   and an hourly refill cron that scans it, classifies what it finds, and
   enqueues the executable items. Fill only: the refill never admits and
   never spawns.
5. **The janitor and the control crons** — a self-contained janitor cron
   that reconciles and admits on a tick, so the queue moves with nobody
   chatting, plus the small inventory of crons that touch admission or
   queue control (the refill, the janitor, and a weekly maintenance job
   that holds the queue through the HOLD file).

Adopt in order (the combined sequence is `templates/install-checklist.md`).
Each layer keeps working when the next one is added: the cap stays in
force for chat dispatch alongside the governor and the janitor, and where
they conflict the stricter limit wins. The governor's original janitor
activation gates were superseded in the reference deployment on
Oct 21, 2026 — see "Gate status — corrected Oct 21, 2026" below; neither
the §11 replay nor the worker-list visibility test is a prerequisite for
the self-contained janitor design.

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
governor is unavailable:

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
governor adds. Keep the cap in force for chat dispatch alongside the
governor and the janitor; the systems apply together and the stricter
limit wins. Retiring the cap is a separate, explicit decision by the
deployment's owner — it is not a side effect of installing the governor
or enabling the janitor, and in the reference deployment it has not
happened.

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

### Brief lint — the artifact standing rule, enforced

`brief_lint.py` (stdlib-only) checks a brief before enqueue: a brief whose
output becomes an artifact, dashboard, UI, widget, wishlist, page, tracker,
or infographic must carry Pete's standing artifact instruction verbatim.
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

## Gate status — corrected Oct 21, 2026

Version 0.1.0 recorded both janitor activation gates as CLOSED. That is
no longer the state of the reference deployment, and the record is
corrected here rather than left standing:

| Gate | Status at v0.2.3 | What changed |
|---|---|---|
| §11 load replay (`REPLAY.md`) | **Waived as a janitor gate** by the deployment's owner, Oct 21, 2026 | The replay remains a useful validation procedure for a quiet host. It is not a prerequisite for the janitor, and it was never run as a gate. |
| Cron-session visibility smoke test | **Failed, and superseded** | Failed Oct 19, 2026 and twice more on Oct 21, 2026 (evidence below). The original janitor design reconciled from the platform's worker listing; the self-contained janitor (Mechanism 4) removes that dependency. The gate's purpose — never over-admit while blind — is preserved by the janitor's conservative rules, not by the test. |
| Interim concurrency cap | **Still in force** for chat dispatch | Enabling the janitor did not retire it. Cap and governor apply together; the stricter limit wins. |
| Janitor | **Active in self-contained conservative mode** in the reference deployment | The `governor-janitor` cron (every 10 min) was created and enabled Oct 21, 2026, with its governor helpers (`janitor-snapshot`, `admit --block-build`, `requeue-interrupted`) implemented and tested in the live workstream. |

A wart that has since closed: `governor.py status` reads its gates line
from the `gates.janitor_cron_active` flag in `limits.yaml`, which the
janitor build was not permitted to change, so the line printed "janitor
cron NOT active" after activation. The flag was flipped to `true` in a
separate permitted change later on Oct 21, 2026, and the gates line now
correctly reads "janitor cron ACTIVE".

## Mechanism 3 — the backlog: the Work Tracker and the refill cron

The governor queue coordinates work that is ready to run. It is not a
backlog: items enter it when someone (or some cron) has decided the work
should be done soon. The backlog of record lives one layer up, in a
**Work Tracker**, and an hourly **refill cron** feeds executable backlog
items into the queue.

### The Work Tracker

A web-artifact tracker (reference Space slug:
`llm-service-manager-work-tracker`, generalized from a single-project
tracker on Oct 20, 2026). Its record shape:

| Field | Values |
|---|---|
| `id` | Auto-incrementing integer |
| `title` | Short work-item title |
| `project` | **General** or **LLMdil** |
| `workstream` | One of: Releases, Core and Gateway, UI and Design, Windows Services, MCP Capabilities, Admin API, Android App, Optimize, Prometheus Hardware, Backlog, General |
| `status` | **Requested**, **Pending**, **In Progress**, **Blocked**, **Done**, **Future** |
| `details` | Free text: context, links, completion conditions |
| `created_at` / `updated_at` | Timestamps |

Operating facts that matter to the refill and to agents reading the
tracker:

- **Projects are a filter, not separate trackers.** The project tabs are a
  multi-select filter; **General is the default project** — the default
  tab selection and the default for newly created items.
- Board view groups items by the six statuses in the order above; table
  view is sortable by id, title, project, workstream, status, and
  updated time. Workstream and status are also filters, and search covers
  the record fields.
- Actions: list items (returns the items plus the project, workstream,
  and status enumerations), create item, update item, delete item. Read
  the tracker through these actions or artifact inspection — never by
  reading its database file directly.
- The tracker is the backlog of record; the governor queue is execution
  coordination. The refill reads the tracker but never edits it. Keeping
  tracker statuses current remains the item owner's job.

### The refill cron (`governor-queue-refill`, hourly)

The refill exists so the queue does not depend on someone remembering to
promote backlog items by hand. Its boundary is strict and is a design
rule, not a workaround for a closed gate: **the refill fills only. It
never runs an admission pass and never spawns — even with the janitor
active.** Admission belongs to chats and the janitor. The full cron body
is `templates/refill-cron-body.md`; the operating summary:

1. **Idle gate.** The refill scans only when the system is genuinely
   idle — all four: no HOLD file; `governor.py status` shows 0 running
   and 0 admitted; the queue is empty (0 queued); load1 is below the
   circuit-breaker threshold configured in `limits.yaml` (read from the
   status output — the threshold is tuned per deployment and has changed
   over time; never hardcode it). If any check fails, the run logs and
   ends silently. This gate is why a stuck queue starves the refill: on
   Oct 20–21, 2026, three stale governor entries held running/admitted
   slots for ~16 hours, the refill's idle gate never opened, and no
   backlog item was enqueued all night. Stale-entry hygiene (the
   janitor's reconciliation) is what keeps the refill fed.
2. **Scan.** The Work Tracker is the primary source; the refill also
   scans any other tracker artifacts it can identify and open runtime
   tracked items/goals, reading each candidate's linked detail (goal
   document, artifact record). It skips items that are completed,
   cancelled, blocked on an external party with no action available, or
   explicitly waiting on the owner in the tracker itself.
3. **Classify** each candidate into exactly one class:
   - **EXECUTABLE** — a self-contained worker brief can be written from
     the tracker entry plus its linked detail without inventing a
     decision the owner has not made: concrete outcome, identifiable
     scope and workspace, checkable completion condition.
   - **NEEDS PETE** — the next step is a decision, approval, purchase,
     booking, send, deletion, or policy choice belonging to the owner.
     Never enqueued, and not council material. Work that spends money,
     books, sends messages, deletes data, or changes an account is
     NEEDS PETE by definition.
   - **ILL-DEFINED** — plausibly executable but missing scope, outcome,
     or completion condition, and not blocked on an owner decision.
     These go to council (below).
4. **Enqueue** executable items, at most **5 per run** (council-refined
   items count toward the cap):
   - Duplicate checks first, against governor state (same item key, same
     tracker id, or near-identical title, live or recently finished) and
     against the refill's own dispositions file — an item already
     dispositioned as enqueued is skipped.
   - The brief is written to `briefs/refill-<item-slug>.md` with the
     outcome, scope, source tracker reference, completion check, and
     constraints from the detail documents.
   - Default class **P3**; **P2** only when the tracker marks the item
     urgent or a deadline falls within 48 hours. Resource is `build` if
     the work compiles or test-gates code, else `light`.
   - Every enqueue carries a **stable idempotency key**:
     `refill-<source>-<tracker item id or slug>`. The key is also the
     item's key in the refill's dispositions file
     (`hidden_files/refill-state.json`, with the run log in
     `hidden_files/refill-log.md`), so re-runs disposition instead of
     duplicating.
5. **Council refinement** for ill-defined items, at most **2 councils
   per run**, skipping items sent to a council in the last 7 days. The
   council (classic panel, 2 rounds) gets the tracker item verbatim plus
   its detail context and one question: define this into an executable
   brief — outcome, scope, completion check — or rule that an owner
   decision is required and state exactly which decision. A refined brief
   at **≥ MEDIUM confidence** is written and enqueued under the normal
   rules. A Pete-decision ruling, or confidence below MEDIUM, is
   recorded as a `needs-pete` disposition with the exact decision
   question, and nothing is enqueued.
6. **Report** to the main chat only when the run did something: enqueued
   items (title, class, resource, source tracker, governor item id) and
   Needs-Pete items with the exact pending decision. Each Needs-Pete
   item is reported once; it is not re-reported unless 7 days have
   passed or its tracker entry changed. Not-idle runs, empty scans, and
   fully-dispositioned scans stay silent.

## Mechanism 4 — the self-contained janitor

The governor is a metering layer, not an executor: before the janitor,
admission passes happened only when a chat dispatched or closed out
work. On the night of Oct 20–21, 2026 the queue demonstrated the gap —
queued items sat unadmitted all night even with load near idle, because
no chat ran a pass, and stale running/admitted entries (workers long
finished, never closed out) held slots the whole time. The janitor is
the cron that closes that gap.

### Why it is self-contained: the visibility verdicts

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

### The design

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

### Reconciliation and the stale-entry rules

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

### Checkpoint heartbeats

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

### Conservative admission

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

### Ownership and supervised mode

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

### Implementation prerequisite

The janitor depends on four governor commands — `janitor-snapshot`
(with `--checkpoints` and `--tracker-json`), `admit --block-build`,
`requeue-interrupted`, and `extract-checkpoints` — plus the
`tracker_ref` item field and the worker-side `checkpoint-line` and
`set-tracker-ref` commands. The `governor.py` shipped in this skill
(v0.2.3) includes all of them, with unit tests (snapshot protection
rules including checkpoints, checkpoint-line generation and refusals,
mid-paragraph marker parsing, malformed-checkpoint flagging, the
block-build pass, the /proc compiler scan, interrupted requeue under
the attempt cap, and an end-to-end phantom-reaping flow). If you port
the governor instead of adopting the shipped file, port that behavior,
with tests, before enabling the janitor cron from
`templates/janitor-cron-body.md` — the checklist
(`templates/install-checklist.md`) makes it a gate.

## Admission/control cron inventory

Every cron that touches admission or queue control, with its role and
its boundary. Scheduled jobs that merely consume reservations (email
triage, scan sweeps, reminders) are not control crons: the governor
cannot admit, delay, or preempt them, and they never touch the queue.

| Cron | Cadence (reference) | Role | Boundary |
|---|---|---|---|
| `governor-janitor` | Every 10 min | Admit, spawn (max 2/tick), reconcile, close out its own workers | Never enqueues; never reads the worker listing; HOLD stops its tick; state + /proc evidence only |
| `governor-queue-refill` | Hourly | Fill the queue from the trackers when the governor is idle | Fill only — never admits, never spawns, never edits a tracker |
| `weekly-binary-updates` | Sundays ~03:00 local | Toolchain maintenance (Rust toolchain, sccache, mold, Python environments) under a queue HOLD | Not an admission authority: it enqueues/starts its own governor item when a pass admits it, but the HOLD file is the operative mechanism; it waits for a compiler-quiet window and removes only its own HOLD |

### The HOLD protocol

- **Who may write it:** any operator or job that must pause admissions —
  in practice the weekly binary-update job, and the owner or a chat
  acting on the owner's instruction.
- **Contents:** the holder writes its reason and start time (an ISO
  timestamp) inside `<governor-dir>/HOLD`. The weekly job writes the
  timestamp and its reason as the file's content.
- **Effect:** while HOLD exists, every admission pass admits nothing and
  the janitor's scheduled tick stops after its log line. Chat dispatch
  also checks for it before running a pass.
- **Removal:** the holder removes its own HOLD when done — and only its
  own. A job that finds someone else's HOLD in place does not overwrite
  it: the weekly job aborts if the existing HOLD is under 3 hours old.
- **Staleness:** a HOLD older than **3 hours** is stale. Any chat may
  remove a stale HOLD after noting the fact in its reply; the weekly job
  notes a stale HOLD, removes it, and then writes its own.

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
5. Run the test suite (`python3 -m unittest test_governor`). The §11
   replay (`REPLAY.md`) is an optional validation run for a quiet host;
   since Oct 21, 2026 it is not an activation gate for anything.

## Setting up AGENTS.md

The rules only work if every future session loads them. `AGENTS-TEMPLATE.md`
in this skill is a ready-to-paste AGENTS.md section covering: the dispatch
rule, the parallelism default with the composition clause, the interim
cap, the governor procedure, the queue hold and HOLD protocol, the
backlog/refill/janitor rules and the gate corrections, the home-directory
deployment rule, and setup notes (where the governor files live, how
`state.json` and `queue.jsonl` relate, how to check status, how to adapt
the numbers). Fill in its placeholders (governor directory, your tuned
limits, and the automation placeholders: tracker slug, governor goal
workspace, council skill path, owner goal, main chat id) and paste it
into the AGENTS.md your runtime loads.

## Install

The full sequence, with verification at each step, is
`templates/install-checklist.md`. In outline:

1. Copy this skill folder to your skills directory (reference:
   `~/workspace/custom-skills/muse-parallel-work/`).
2. Create the governor directory **under home** (reference:
   `~/workspace/parallelism-governor/`) and copy into it: `governor.py`,
   `test_governor.py`, `limits.yaml`, `DESIGN.md`, `REPLAY.md`. The
   script creates `queue.jsonl`, `state.json`, `briefs/`, and
   `metrics.jsonl` on first use.
3. Tune `limits.yaml` per the procedure above.
4. Verify: `cd <governor-dir> && python3 -m unittest test_governor`
   (72 tests) and `python3 governor.py status`.
5. Paste the filled-in `AGENTS-TEMPLATE.md` section into your AGENTS.md.
   Operate under the interim cap + governor procedure together from here
   on; the cap remains in force for chat dispatch through every later
   step.
6. Stand up the Work Tracker (projects General/LLMdil, the six statuses,
   the workstream enumeration) as the backlog of record.
7. Create the refill cron from `templates/refill-cron-body.md` (hourly,
   fill only) and run one supervised pass.
8. Run the visibility tests and record the evidence. A FAIL blocks only
   the old visibility-based janitor design; the self-contained design
   proceeds. Then port the janitor helper commands if your governor
   build lacks them, create the janitor cron from
   `templates/janitor-cron-body.md`, and run one supervised first tick
   before leaving it enabled.

## Files in this skill

| File | Role |
|---|---|
| `SKILL.md` | This playbook. |
| `AGENTS-TEMPLATE.md` | The AGENTS.md section to paste into your runtime's instructions. |
| `README.md` | Human-facing summary: what it is, the incident, install, gate status. |
| `governor.py` | The reference implementation (stdlib-only Python 3). |
| `brief_lint.py` | Rejects governor briefs whose output becomes an artifact/UI but lack Pete's standing artifact instruction; `--fix` appends it. |
| `test_brief_lint.py` | Unittest suite for brief_lint.py. |
| `test_governor.py` | 72-test unittest suite for governor.py. |
| `limits.yaml` | Template limits with tuning guidance. |
| `DESIGN.md` | The full design and decision record, incl. the council-modifications addendum and the v0.2.0 status correction. |
| `REPLAY.md` | The §11 validation procedure (optional validation since Oct 21, 2026; not an activation gate). |
| `templates/janitor-cron-body.md` | The janitor cron body, with placeholders — the self-contained conservative design as a deployable template. |
| `templates/refill-cron-body.md` | The refill cron body, with placeholders — idle gate, classification, council refinement, idempotent enqueue. |
| `templates/install-checklist.md` | The combined install checklist: governor → tracker → refill → janitor, with verification steps. |
| `examples/example-brief.md` | A self-contained item brief, as written at enqueue. |
| `VERSION` | Skill version. |
