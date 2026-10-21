# Parallelism Governor — Design

> Carried over from the reference deployment, where this design was approved
> (via a council review, Option B, with the modifications recorded in the
> addendum at the end) and implemented as `governor.py` in this skill.
> Deployment-specific names have been generalized; the reasoning, numbers,
> and decision records are unchanged. In the reference deployment the
> interim AGENTS.md concurrency cap (max 4 build-heavy, max 8 light) stayed
> in force alongside the governor under the activation-gate framing
> recorded in the council-modifications addendum; that framing is
> superseded by the v0.2.0 status correction at the end of this document
> (the cap itself remains in force for chat dispatch).

## 1. Problem

On Oct 18, 2026, 14+ subagents ran concurrently across chats — feature implementations, a design task, a docs refresh, a competitive analysis, and a code-review coordinator that itself fanned out 11 sub-reviewers — plus scheduled scan classifiers. Most were compiling Rust. The VM saturated: measured load average hit 17.6 on a 2-vCPU box (~9x oversubscribed). the user could not send messages in some chats, and replies took very long. Chat, reminder workers, and batch workers all share the same runtime and inference queue, so batch overload directly degrades interactivity.

The failure was not any single dispatch. Each chat agent followed the standing "parallelism default" (fan out aggressively) and the dispatch rule (keep the chat turn brief, delegate the work) in isolation. Nothing anywhere counted the total, priced the hardware, or ordered competing requests against each other. The interim cap fixed the count crudely. This design replaces the crude cap with a system:

- Requests carry a **priority class** based on urgency, importance, and latency sensitivity.
- Work waits in a **durable queue** (files under `~/workspace/`, surviving VM replacement) and is admitted in priority order.
- Admission is limited by **resource classes derived from measured hardware**, not by a single global number.
- Batch work still saturates idle capacity; it just never takes the capacity chat needs.

Non-goals for v1: preempting running work (prohibited by standing rule — priority governs admission order only), a platform scheduler (none exists), and guaranteeing enforcement against an agent that ignores the rules (see §12, Risks — enforcement is by convention and shared state, and the design is honest about that).

## 2. Measured facts (verified Oct 18, 2026, 23:12 EDT)

| Fact | Value | Consequence |
|---|---|---|
| vCPUs | 2 (AMD EPYC 9D64 host) | At most ~2 CPU-bound threads make progress at once |
| RAM | 7 GB total, ~4 GB available under load | Memory, not just CPU, bounds concurrent builds |
| Incident load average | 17.6 (~9x oversubscribed) | Runnable-queue collapse, not mild contention |
| Cargo behavior | Parallelizes internally across cores; individual rustc processes can each exceed 1 GB RSS | One build already uses the whole box; two builds contend hard and risk swap/OOM |
| VM lifecycle | VM replaced frequently by deployments; ad-hoc processes die; runtime crons/jobs are recovered | All durable state must be files under `~/workspace/` |
| Platform | No scheduler API; chat agents spawn subagents directly; max spawn depth 2; coordinators fan out | The governor is a discipline + shared state, not a daemon with real control |

## 3. How this composes with the standing rules (AGENTS.md)

- **Dispatch heavy work to subagents** — unchanged. Chat agents still dispatch and stay conversational. The governor sits between "decided to dispatch" and "spawn": dispatch becomes *enqueue, then admit when a slot fits*. The chat agent's acknowledgment stays one line; it gains queue position when the item is not admitted immediately (§9).
- **Parallelism default** — narrowed, not repealed. Fan-out is still the default *within the admitted budget*. "Parallelize where possible" now means "enqueue all the independent pieces; the governor runs as many at once as the hardware serves well." A coordinator's fan-out is enqueued as individually counted items (§5).
- **Concurrency cap (interim)** — superseded by §4's resource limits on Phase 1 rollout. Its two hard rules carry over verbatim: never kill in-flight work to enforce limits retroactively, and fan-out counts against the budget.
- **Promised follow-ups need a scheduled check** — the queue's janitor (§7) is a real cron, which is what makes "it will start when a slot frees" a kept promise instead of a hope. No agent promises to watch the queue without that cron behind it.
- **Long background jobs: checkpoint + resume** — every queue item carries a checkpoint/resume pointer in the established form (a `~/workspace/.resume/<job-name>.md` marker or a worktree/branch state), and the recovery procedure (§10) is built on reading those markers first.

## 4. Resource model: slots derived from the hardware

Three budgets, all enforced at admission time. A worker consumes from each budget it belongs to for its whole life (a feature-implementation worker holds its build slot through its build *and* its test gates — gates are phases of the item, not separate admissions).

### 4.1 The budgets

| Budget | Limit | What consumes it |
|---|---|---|
| **Build slots** | **1** | Any worker that compiles or runs heavy test gates (cargo/npm builds, cargo test/clippy gates, release pipelines) |
| **Light slots** | **4** | Workers that do not build: research, docs, reads, analysis, review, design |
| **Total worker slots** | **5** | Every running subagent worker, whatever its class (coordinators included; a coordinator's children count individually) |

### 4.2 The arithmetic

- **Build = 1.** the user's rule of thumb — no more parallel compiles than cores − 1 — gives 2 − 1 = 1 on this box. The RAM math independently gives the same answer: ~4 GB available under load, and one large Rust workspace build peaks at multiple GB across its parallel rustc processes (each can exceed 1 GB). One build gets a ~2.5–3 GB envelope; a second concurrent build does not fit beside it plus the runtime. A single cargo build parallelizes internally, so one build slot still uses both cores fully — the slot costs almost no throughput versus thrashing two builds at half speed plus swap.
- **Can a build and a test gate overlap?** Not in v1. The gate's compile phase needs the same build resources; its run phase is CPU-parallel across both cores and its test binaries add ~1 GB+ beside a build's ~3 GB. On 7 GB total that is an OOM risk to save wall-clock minutes. Phase 3 may test a gate-run overlap against measured RSS; v1 keeps one build token covering compile and gates end to end.
- **Light = 4.** Light workers are latency-bound on network and inference, not CPU-bound; each is mostly waiting. Four of them add little runnable load and ~150–250 MB each in agent/tool footprint (~1 GB total), which fits beside one build inside the available RAM.
- **Total = 5.** the user's suggested starting point, and it falls out of the sub-limits anyway (1 build + 4 light = 5). The total is the backstop that keeps mixed loads honest: e.g., 5 light workers with no builder still consume all 5 slots, so a sixth waits.
- **Headroom for P0 (chat/runtime).** The caps are set so the *admitted worst case* is ~2 runnable CPU-heavy threads (one build's internal parallelism) plus light churn — target steady-state load average ≤ ~3, versus 17.6 in the incident. Chat responsiveness is protected structurally by this reservation, not by giving chat a slot: chat is never queued, and the budgets are sized so a full queue cannot recreate the incident. If load average is observed above ~5 during an admission pass, the pass admits nothing new until it falls (a load circuit-breaker, checked from `/proc/loadavg`, costing nothing when idle).

## 5. Priority model

### 5.1 Classes

| Class | Meaning | Queued? | Examples |
|---|---|---|---|
| **P0** | Interactive chat itself | Never | the user's messages, quick single-step checks in-chat |
| **P1** | Time-critical, due now, user-facing | No — bypasses the queue | Medication reminders firing, safety-alert handling, watchdogs, email triage when its hour comes, anything the user marks urgent in chat |
| **P2** | the user is actively waiting on it | Yes, head of queue | A diagnostics read blocking the user's next test step, a small fix mid-testing-session, a release gate for a build they are about to install |
| **P3** | Default batch work | Yes | Feature implementations, release work, research the user asked for and will read later |
| **P4** | Background, can slip | Yes | Full code reviews, docs refreshes, competitive analysis, clearance scans, newsletter digests |

P1 needs care: most P1 work is runtime crons, which the governor cannot admit or delay (§8) — P1 classification for them is accounting, not control. For *interactive* P1 (the user in chat says "check this now"), the rule is: run immediately, borrowing the P0 headroom, and pause new admissions until it completes. P1 never kills or preempts running batch work; it only jumps the queue and temporarily closes the admission gate.

### 5.2 Who assigns the class

1. **Defaults per work type** (table in §5.3) — applied by the dispatching agent without asking.
2. **The dispatching chat agent's judgment** — context the table cannot see: the user is mid-testing-session and blocked (→ P2), the user said "no rush" (→ P4).
3. **The user's words override everything.** "Urgent", "right now", "I'm blocked on this" → P1/P2 as appropriate; "whenever", "background", "no rush" → P4. The user can also reclassify a queued item after the fact ("move the review behind the release") and the queue obeys. (Decision D5.)

### 5.3 Default classes for recurring work

| Work type | Default class | Resource |
|---|---|---|
| Feature implementation / fix | P3 (P2 if the user is actively testing and waiting) | Build |
| Release merge + gates | P2 | Build |
| Diagnostics the user requested while testing | P2 | Light (usually) |
| Deep research / competitive analysis | P3 if the user will act on it soon, else P4 | Light |
| Full code review (coordinator + fan-out) | P4 | Light, fan-out counted per child |
| Docs refresh | P4 | Light |
| Medication reminders, alert watchers, household watchdog | P1 | Runtime cron — budgeted, not queued |
| Email triage (hourly) | P1 when due | Runtime cron — budgeted as 1 light-equivalent |
| Multi-classifier scan sweeps | P4 | Runtime cron — budgeted as ~3 light-equivalents during a sweep |
| Daily scans | P4 | Light |
| Newsletter digest (weekly) | P4 | Light |
| RAM/listing watches | P4 | Light |

### 5.4 Aging (starvation bound)

Queued time raises effective priority: **+1 class per 60 minutes queued** (P4 → P3 after 1 h, → P2 after 2 h), capped at P2 — aging never manufactures P1. Within an effective class, order is FIFO by enqueue time. Consequence: under continuous load, a P4 light item starts within ~2 hours of enqueue in the worst case, and a P4 build item within ~2 hours plus one build duration. Nothing waits forever behind a stream of fresh P3 work. (Decision D7.)

### 5.5 Fan-out accounting

A coordinator does not hold one slot for its whole fan-out. Each child consumes its own slot and is admitted individually: the coordinator enqueues a bundle (`fanout: N` child items, or N separate items with a shared `bundle` id), children start as slots free, and the coordinator itself consumes 1 light slot while it waits and synthesizes. The Oct 18 review would have run its 11 reviewers ~4 at a time instead of all at once — slower per-review in wall clock, bounded in load, and the synthesis still completes. (Decision D9.)

## 6. Queue mechanics

### 6.1 Where it lives

All under the governor directory, which lives under the home directory (home persists across VM replacement; see the skill's deployment location rule):

| Path | Role |
|---|---|
| `limits.yaml` | The budgets from §4 and the cron reservations from §8. The single place numbers change. |
| `queue.jsonl` | Append-only event log (enqueue, reclass, admit, start, finish, fail, interrupt, cancel). Source of truth; never edited in place. |
| `state.json` | Materialized current state (queued + running), rewritten atomically (temp file + rename). Rebuildable from `queue.jsonl` at any time. `state.prev.json` keeps the last good copy. |
| `items/<id>.md` | One file per item: the full self-contained task brief, exactly as a subagent would receive it, plus preconditions and the checkpoint/resume pointer. |
| `lock/` | Mutex via atomic `mkdir` (see §6.4). |

Why files, not a service: the VM is replaced frequently, ad-hoc processes die, and every agent can already read and write files. A daemon would be one more thing that dies silently; the queue as files is inspectable by the user and by any agent with no tooling beyond what exists today.

### 6.2 Item schema

```yaml
id: 20261018-2215-optimize-plan-fixes-a3f2   # date-time-slug-short-hash
title: "Optimize plan fixes (3 defects)"
class: P3                # assigned class; effective class computed with aging
resources:
  build: true            # needs the build slot
  est_ram_gb: 3          # advisory; used by observability, not admission in v1
  fanout: 1              # >1 only on a bundle parent
owner:
  chat_id: <chat-id>  # the chat that asked for the work
  chat_name: "Feature development"
enqueued_at: 2026-10-18T22:15:00-04:00
state: queued            # see §6.3
brief: items/20261018-2215-optimize-plan-fixes-a3f2.md
resume_pointer: ~/workspace/.resume/optimize-plan-fixes.md   # or worktree path
depends_on: []           # item ids that must finish first
idempotency_key: "llmdil:feature/optimize-plan-fixes"        # see §10
attempts: 0
worker_agent_id: null    # set at start
admitted_at: null
last_error: null
```

The brief file is what makes the queue real: it contains everything a worker needs (task, repo, branch, constraints, gates, report-back requirements) so that *any* dispatcher session — not only the chat that originated the request — can start the item correctly hours later.

### 6.3 States and transitions

```
queued ──admit──▶ admitted ──spawn──▶ running ──▶ done
  │                  │                    ├──────▶ failed
  │                  │ (reaped: stale     ├──────▶ interrupted (VM/agent gone)
  │                  │  reservation)      │            │
  │◀─────────────────┘                    │            ▼
  │                                       │      requeued (if resumable) or surfaced to owner
  ├──▶ blocked (dependency unfinished; auto-clears when deps are done)
  ├──▶ stale (preconditions no longer hold; owner chat must refresh or drop)
  └──▶ cancelled (the user or owner chat withdraws)
```

- **admitted** is a reservation, not a spawn: the admission pass marks slots taken *before* the spawn call returns, which is what makes concurrent admission passes from different chats safe (§6.4).
- **blocked** items consume no slots and are re-checked on every admission pass.
- **stale** is the honest state for "the world moved": e.g., a feature item briefed against an older main commit after two more releases shipped. Workers revalidate the brief's stated preconditions at start; on failure they mark stale and notify the owner chat instead of running blind.

### 6.4 Concurrency control

Every mutation (enqueue, admission pass, state change) happens under the lock: `mkdir lock/` succeeds for exactly one holder; the holder writes, then removes it. Critical sections are file operations only (milliseconds). A lock whose mtime is older than 60 seconds is considered abandoned (holder died mid-write) and may be stolen. All admissions follow the same shape:

1. Take lock. Read `state.json` (rebuild from `queue.jsonl` if unreadable).
2. Reconcile against reality: `subagent.list` for live workers (§10), runtime cron reservations (§8), `/proc/loadavg` circuit-breaker.
3. Compute free budgets. Walk the queue in effective-priority order; for each item whose resource needs fit and whose dependencies are done, mark it `admitted` and decrement the budgets. Skip (don't stop at) an item that doesn't fit — a queued build item must not block light items behind it.
4. Append events to `queue.jsonl`, rewrite `state.json`, release lock.
5. Spawn the admitted workers *outside* the lock (spawning is slow; holding the lock across it would serialize chats). The reservation in step 3 is what prevents double-admission in the gap.

## 7. Dispatch and admission: who promotes the queue

Three realistic options:

**(a) Admission control only.** No queue file as a work source; each chat agent, before spawning, checks `subagent.list` + load and either spawns or tells the user "queued" and relies on a future event to start it. *Tradeoffs:* zero new machinery, results return naturally to the requesting chat. But nothing promotes queued work when all chats are idle — "queued" items start only when some later conversation happens to trigger them, which breaks the follow-through rule. Enforcement is purely per-agent memory. This is the interim cap with better manners, not a queue.

**(b) Dispatcher cron.** A runtime cron (every few minutes) reads the queue, runs the admission pass, and spawns workers itself. *Tradeoffs:* queued work progresses with nobody chatting — the real queue property. But a cron session is an agent in its own context: workers it spawns report to *it*, not to the user's chat, so results must be re-routed (the cron agent posts results into the owner chat via `chat.send_message` — workable, but an extra hop where context can be lost). A frequent tick burns inference around the clock even with an empty queue. Double-dispatch risk if chat agents also admit on their own — so under (b), chat agents must *only* enqueue, which adds up-to-one-tick latency (minutes) to every P2 request the user is actively waiting on. That latency is exactly what P2 exists to avoid.

**(c) Hybrid — recommended for v1.** Enqueue always; admit opportunistically; janitor as backstop.

- Every dispatch from any chat = write the item + brief, then immediately run one admission pass (§6.4). In the common case (slots free) the item is admitted and spawned in the same turn — the user sees no added latency, and the acknowledgment is unchanged from today.
- A **janitor cron**, every 15 minutes, runs the same admission pass and nothing else: promotes queued items as slots free, reaps stale reservations and dead workers (§10), rebuilds state if needed, and exits. It does not spawn long work *as itself* in v1 — it admits, and the spawn is performed by… see the honest limitation below.
- One admission algorithm, one lock, one state file, whoever runs the pass.

*Honest limitation of (c) in v1:* a janitor cron session can run the pass and mark items admitted, but a worker spawned from the janitor's session reports to the janitor, not to the owner chat. Two mitigations, either acceptable: (i) the janitor admits and immediately hands the admitted item to the owner chat via `chat.send_message` with the brief — the owner chat's agent spawns the worker in its own context, so results land where the user expects (cost: the owner chat must be woken, which `chat.send_message` does); or (ii) the janitor spawns the worker and, on completion, posts the full result to the owner chat. The design recommends (i) — result routing stays exactly as today, and the janitor stays a pure bookkeeper. If no owner chat can take the item (archived chat, the user offline and the item is P4), (ii) applies and the result posts when the worker finishes.

**v2 adds:** measured auto-tuning of `limits.yaml` (build RSS and scan fan-out measured, not estimated); a full dispatcher if (and only if) v1 shows convention leaking (agents forgetting the pass); dependency-aware bundles (a release item that auto-enqueues its gate item); and the artifact queue board if the user wants one (§9).

## 8. Interaction with scheduled jobs

Runtime crons fire on their own schedule. The governor cannot admit, delay, or preempt them, and this design does not pretend otherwise. What it does:

- **Reservation accounting.** Known cron footprints are declared in `limits.yaml` and counted as occupied budget during their windows, so chat-dispatched admissions don't pile onto a load the crons already created:

| Cron | Footprint counted | Window |
|---|---|---|
| Med reminders, watchdog (2-min), watches | Negligible CPU; not counted | — |
| Email triage (hourly) | 1 light-equivalent | While its run state is active |
| Scan sweep | 3 light-equivalents (its classifier fan-out) | While a sweep is running (detectable from its own run state files) |
| Daily scan | Counts as one queued P4 light item | Enqueued by rule, run by its existing cron |
| Weekly digest | 1 light-equivalent | While running |

- **Known heavy windows.** A multi-classifier scan sweep is the only scheduled job that materially loads the box in the reference deployment. Admission passes during a sweep simply see 3 fewer light slots. No schedule changes are proposed; the governor adapts to the crons, not vice versa.
- **Cron-originated subagents** (camera classifiers, triage fetchers) are spawned by the runtime, outside the queue. They are counted in reconciliation via `subagent.list` (they appear there), so the *effective* budget math stays honest even where the reservation table is approximate.
- **Self-cleaning follow-up crons** (the "promised follow-ups" pattern) are unaffected; they are P1/P2 by nature and tiny.

## 9. Observability

- **At dispatch time**, the chat agent's one-line acknowledgment gains queue information only when the item is *not* admitted immediately:
  - Admitted: "Dispatched: MCP credential labels (build slot)." — as today.
  - Queued: "Received: code review — queued P4, position 3. It starts when the current build and one light item finish; nothing is running over the cap meanwhile."
  Position is computed from effective-priority order at enqueue time and labeled as approximate (aging and P2 arrivals can move it).
- **Status on request.** the user asks "what's queued?" / "what's running?" in any chat; the agent reads `state.json` and answers in a fixed shape:

```
Running (4/5 workers, build 1/1, light 3/4, load 2.4):
  1. [P2 build] v0.1.41 release gates — 22 min — feature chat
  2. [P3 light] Planner-mtp diagnostics — 6 min — main chat
  ...
Queued (3):
  1. [P3 build] #57 empty-plan fix — waiting on build slot
  2. [P4 light] Docs refresh — ages to P3 at 23:40
  3. [P4 light] Code review (bundle of 11, 4 running counted above)
Done in last 24h: 9 · Failed: 1 · Interrupted/requeued: 1
```

- **Artifact board:** deferred (Decision D8). A live queue page is buildable later from `state.json` alone; v1's in-chat status covers the actual need (the user asks in chat; he does not need another tab to babysit). Building the board before the mechanics settle would bake in the wrong schema.

## 10. Failure and recovery

| Failure | Handling |
|---|---|
| **VM replacement mid-flight** | Running workers die; their items sit `running` with dead agent ids. Next admission pass (janitor or any chat) reconciles against `subagent.list`: a `running` item whose agent is absent and which was admitted >10 min ago → `interrupted`. Then the standing rule applies: read the item's resume pointer / `~/workspace/.resume/` first; if the brief marks it resumable and attempts < 2, auto-requeue at its original class; else surface to the owner chat. Remove the resume marker only on real completion. |
| **Stale reservation** | `admitted` with no live agent 5 min after admission → back to `queued`, budgets released. |
| **Duplicate dispatch** | Enqueue checks `idempotency_key` against queued/running items; a match returns the existing item ("already queued, position N") instead of creating a second. For code work the branch/worktree name is the natural key. |
| **Stale context** | Worker revalidates brief preconditions at start (base commit, named files, tracker state). Failure → item `stale`, owner chat notified with specifics; nothing runs blind. the user or the owner chat refreshes the brief or cancels. |
| **Queue corruption** | `state.json` is a cache: torn or unparsable → rebuild from `queue.jsonl` (skipping a torn final line), falling back to `state.prev.json`. The JSONL log itself is append-only, so damage is bounded to the last partial write. |
| **Dead lock holder** | Lock older than 60 s is stealable (§6.4). |
| **Runaway queue** | Janitor reports any item queued >24 h to its owner chat. the user can cancel any item by asking; cancellation is an event in the log, not a file deletion. |
| **Agent ignores the governor** | Detected after the fact in reconciliation (running workers with no queue item). The item-less worker is left alone (no retroactive kills) but the over-admission is visible in status and attributable to a chat. This is the known enforcement limit (§12). |

## 11. Rollout plan

**Phase 1 — rules + queue files, no code.** the user approves this design and the decision list. Create `limits.yaml`, the item template, and empty queue files. Replace the interim AGENTS.md cap section with a reference to this system (the two hard rules — no retroactive kills, fan-out counts — carry over). Admission passes are executed by agents by hand, following §6.4 as a written procedure (read state, reconcile with `subagent.list`, compute, mark, spawn). Validate the §11 replay test once by hand.

**Phase 2 — mechanism.** A small helper script (`governor.py`, python3) that takes the lock, reconciles, computes admissions, and prints the decision, so agents stop doing arithmetic by hand. The janitor cron (every 15 min) runs it. A `status` subcommand produces the §9 report shape verbatim. Admission still executed by agents (the script decides, the agent spawns) — this keeps result routing unchanged.

**Phase 3 — tuning.** Measure what v1 estimated: real build RSS and durations per repo, camera sweep fan-out size, light-worker footprints. Adjust `limits.yaml` from data. Finalize per-job-type class defaults in §5.3 from observed behavior. Optional: artifact queue board; test gate-run/build overlap (§4.2) if measurements show RAM headroom; consider full dispatcher (option b) only if Phase 1–2 show convention leaking in practice.

**Validation (how we know it works).** Replay the Oct 18 load shape under the governor: enqueue the same mix — ~7 build items, 1 review bundle of 11, docs refresh, competitive analysis, a diagnostics P2 — on top of a normal scheduled background. Success criteria, all measured:

1. `subagent.list` sampled every minute never shows more than 5 workers, 1 build.
2. Load average stays ≤ ~3 (vs 17.6 in the incident).
3. A test message sent to a chat mid-run receives a reply within normal interactive latency (target: under ~30 s end to end, versus minutes/unavailable in the incident).
4. Every item completes; the P4 bundle finishes via aging without manual intervention (starvation check).
5. Total batch completion time is no worse than an agreed factor of the unthrottled run — expected to be comparable or better, since unthrottled builds thrash each other — and in any case the interactive criteria (2) and (3) outrank it.

## 12. Risks and honest limits

- **Enforcement is conventional.** There is no platform scheduler. The governor works because every chat agent follows the same written procedure against shared state. One agent that skips the pass can still oversubscribe the box; the design makes that visible and attributable, not impossible. If that proves insufficient in practice, Phase 3's dispatcher is the escalation.
- **Build = 1 serializes release gates behind long builds.** Mitigated by class (release work is P2 and jumps the queue) but not eliminated: a 40-minute feature build delays a P2 gate by up to its remaining runtime. Accepted — the alternative (2 builds) is the incident.
- **Aging can surprise.** A P4 item promoting to P2 after 2 hours may start at an odd moment. Bounded by the 24-hour janitor report and the user's ability to cancel or reclassify.
- **Reservations for crons are estimates.** The camera sweep's true fan-out varies. Reconciliation via `subagent.list` corrects the accounting after the fact; Phase 3 replaces estimates with measurements.
- **The queue is another surface to keep honest.** If items are enqueued and silently abandoned, status becomes fiction. The 24-hour report and the stale state exist to keep the queue a true picture; agents must close out items they complete (the handoff step includes the queue event).

## 13. Decisions for the user

- **D1 — Total worker cap: 5.** (1 build + 4 light, or 5 light.) Alternatives: 4 (safer, slower batch) or 6 (faster batch, thinner chat headroom). **Recommendation: 5** — it is the user's suggested number and the sub-limits arithmetic lands there independently.
- **D2 — Build slots: 1** (= cores − 1; also the RAM-bound answer). Alternative: 2, which reproduces hard contention on this box. **Recommendation: 1.**
- **D3 — Test gates share the build slot** (no build/gate overlap in v1; revisit with Phase 3 measurements). **Recommendation: share.**
- **D4 — Light slots: 4.** **Recommendation: 4**, with the load circuit-breaker (no admissions while load > ~5) as the safety net if 4 proves too many in practice.
- **D5 — the user's urgency words override class defaults** ("urgent/blocked" promotes, "no rush/background" demotes; the user can reclassify queued items at any time). **Recommendation: yes.**
- **D6 — Default classes for recurring work as in §5.3** — notably: feature builds P3 (P2 when the user is actively waiting), releases P2, full code reviews and docs P4, scan sweeps budgeted at 3 light-equivalents, email triage P1-when-due, med reminders P1 (never queued). **Recommendation: adopt the table.**
- **D7 — Aging: +1 class per 60 minutes queued, capped at P2.** Alternatives: 30 min (P4 drains fast, more churn) or 2 h (calmer, longer worst-case wait). **Recommendation: 60 minutes.**
- **D8 — Observability: in-chat status on request for v1; artifact queue board deferred to Phase 3.** Alternative: build the board in v1. **Recommendation: defer the board.**
- **D9 — Fan-out accounting: each child consumes its own slot; coordinators enqueue bundles of counted children.** Alternative: a coordinator holds a single slot for its whole fan-out (simpler, but it is exactly the loophole that produced 11 reviewers at once). **Recommendation: per-child counting.**
- **D10 — Dispatcher mechanism: hybrid (c)** — enqueue always, opportunistic admission by the dispatching chat, 15-minute janitor cron as backstop, janitor hands admitted items to the owner chat to spawn. Alternatives: admission-only (a) or full dispatcher cron (b). **Recommendation: hybrid.**
- **D11 — Recovery: resumable interrupted items auto-requeue (max 2 attempts) per their checkpoint/resume markers; others surface to the owner chat.** Alternative: always surface first and never auto-requeue (safer, but contradicts the standing "resume unfinished work without being asked" rule). **Recommendation: auto-requeue as specified.**
## Council modifications (Oct 18, 2026)

The design was approved via council (Option B, medium confidence) with four modifications, which override the design text above where they differ. Implemented the same night; see STATUS.md for what was built.

1. **`governor.py` is built now (Phase 1), not after a hand-executed trial.** §11's Phase 1 assumed agents would run admission passes by hand as a written procedure before any mechanism existed. The council rejected that: hand-executed admission passes are not the operating mode. The script, the queue store, and the test suite were built in the same task that stood the system up; the operating procedure in AGENTS.md ("Parallelism governor") calls the script from the start.
2. **Promotion order is (effective class, base class, enqueue time).** §5.4 specified ordering within an effective class as FIFO by enqueue time alone, which would let an aged P4 item (effective P2 after two hours) precede a fresh P2 item. The tie-break on base class comes first: at equal effective class, the item whose assigned class is higher goes first; enqueue time only breaks remaining ties.
3. **The lock-steal threshold comes from measured pass durations, not the 60 s constant in §6.4.** The threshold is a multiple (10x) of the observed p95 admission-pass duration, with a 5 s floor, and applies the floor alone until at least 5 passes have been measured. Pass duration is instrumented from the first run (`metrics.jsonl`). Consequence: a lock is presumed abandoned after seconds, not a minute, because real critical sections are file operations measured in milliseconds — and a waiter whose wait budget expires before the steal threshold fails cleanly with no state change.
4. **Two activation gates remain CLOSED.** (a) The janitor cron (§7) is not created yet — until it exists, admission passes happen only when a chat dispatches or closes out work, and queued items wait for the next pass rather than being promoted on a 15-minute tick. (b) The interim AGENTS.md cap is not retired — it and the governor procedure apply together, stricter limit winning, until both gates open. The gates open on: the §11 replay passing, and a cron-session visibility smoke test (a one-shot cron confirming that a cron session's `subagent.list` sees the same worker population the governor reconciles against — if cron sessions see a different population, reconciliation from the janitor would be unsound).

## v0.2.0 status correction (Oct 21, 2026)

This section supersedes the gate status recorded in the council
modifications addendum (item 4) and the janitor framing in §7 and §11,
which stand as the Oct 18–19, 2026 decision record:

- The cron-session visibility smoke test was run on Oct 19, 2026 and
  FAILED (a cron session's `subagent.list` returned 0 agents while work
  was verifiably running). It was retested twice on Oct 21, 2026 and
  failed both times: a cron session saw 0 agents in both scopes while
  the governor reported 5 running/admitted, and an agent spawned by a
  cron session also saw 0 — not its parent, not itself, not other chats.
  Blindness follows the cron lineage; spawning does not escape it.
- The owner waived the §11 replay as a janitor activation gate on
  Oct 21, 2026. The replay remains a validation procedure (REPLAY.md),
  not a gate.
- The owner approved a self-contained janitor design the same day, and
  the `governor-janitor` cron is active in that mode: every 10 minutes
  (not the 15 in §7), spawning its own workers and closing them out on
  handoff (closer to §7's option (b) mechanics than the hybrid's
  hand-to-owner-chat mitigation), reconciling from the queue log plus
  /proc evidence — never `subagent.list` — under conservative rules:
  state-derived admission snapshot, unexplained-compiler guard on build
  admissions, HOLD file, load breaker, slot limits, and a cap of 2
  spawns per tick. The full as-built description is in SKILL.md
  (Mechanism 4) and `templates/janitor-cron-body.md`.
- The interim AGENTS.md cap is NOT retired. It remains in force for chat
  dispatch, alongside the governor and the janitor.
