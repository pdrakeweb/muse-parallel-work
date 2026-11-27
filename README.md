# muse-parallel-work

A skill for managing **parallel agent work on a shared machine**: how an
agent system fans work out to sub-agents without saturating the host it is
also trying to talk to its user on.

It has five layers, in adoption order:

1. **Principles** — the chat agent dispatches heavy work to sub-agents and
   stays conversational; fan-out is the default, exercised inside an
   admitted budget; budgets are derived from measured hardware.
2. **The interim concurrency cap** — a no-tooling mechanism: separate
   build-heavy and light budgets, a coordinator's fan-out counted per
   child, never kill in-flight work to enforce the cap.
3. **The parallelism governor** — the reference metering implementation:
   a durable file-based queue with priority classes (P0–P4), admission
   passes against build/light/total budgets, aging so nothing starves, a
   load circuit breaker, and reservations for scheduled jobs. Included
   here as working code (`governor.py`, stdlib-only Python) with a
   21-test suite, the full design record (`DESIGN.md`), and the
   validation procedure (`REPLAY.md`).
4. **The backlog and refill** — a general Work Tracker (projects
   General/LLMdil, six statuses, workstreams as fields and filters) as
   the backlog of record, and an hourly refill cron that scans it when
   the governor is idle, classifies items EXECUTABLE / NEEDS USER /
   ILL-DEFINED (ill-defined items go to a council; a refined brief at
   ≥ MEDIUM confidence is enqueued), and enqueues under stable
   idempotency keys. Fill only — the refill never admits or spawns.
5. **The janitor and control crons** — a self-contained janitor cron
   (every 10 minutes) that owns its workers end-to-end and reconciles
   from the queue log plus /proc evidence — never the worker listing,
   which cron-lineage sessions verifiably cannot see — under
   conservative admission rules (state-derived snapshot,
   unexplained-compiler guard, HOLD file, load breaker, 2-spawn-per-tick
   cap), plus the admission/control cron inventory and the HOLD
   protocol.

## Why it exists

On Oct 18, 2026, 14+ sub-agents ran concurrently on a 2 vCPU / 7 GB VM —
most of them compiling Rust — plus a code-review coordinator's 11
sub-reviewers and scheduled scan jobs. Load average hit 17.6, roughly 9x
oversubscribed. The user could not send chat messages; replies took
minutes. Every agent involved had followed the standing instructions
(dispatch, fan out) correctly in isolation. Nothing counted the total,
priced the hardware, or ordered competing work. This skill is the system
built from that incident.

## Install

1. Copy this folder into your agent's skills directory.
2. Create a governor directory **under your home directory** (for example
   `~/workspace/parallelism-governor/`) and copy `governor.py`, `brief_lint.py`,
   `test_governor.py`, `test_brief_lint.py`, `limits.yaml`, `DESIGN.md`, and `REPLAY.md` into
   it. The governor creates its queue files (`queue.jsonl`, `state.json`,
   `briefs/`, `metrics.jsonl`) on first use.

   **Home directory only.** Do not install any part of this system — or
   the toolchains it coordinates — under `/usr`, `/usr/local`, or `/etc`.
   On this class of VM those trees are wiped on replacement; the
   reference deployment lost a build cache and linker installed in
   `/usr/local` to exactly that, under an hour after installing them.
3. **Tune `limits.yaml` to your hardware.** The shipped numbers
   (build 1, light 4, total 5, breaker at load 5.0) are calibrated to a
   2 vCPU / 7 GB VM where one build already saturates both cores.
   Measure your host first — vCPUs, RAM, one real build's peak — and
   scale by the arithmetic in the template's comments, not by preference.
4. Verify: `python3 -m unittest test_governor` (81 tests; plus `test_brief_lint`, 9 tests) and
   `python3 governor.py status`.
5. Paste the filled-in section from `AGENTS-TEMPLATE.md` into the
   AGENTS.md your agent runtime loads. That template carries the dispatch
   rule, the fan-out default, the interim cap, the governor procedure,
   and the deployment location rule — the instructions future sessions
   will actually follow.
6. Then stand up the backlog layers in order — Work Tracker, refill
   cron, janitor — following `templates/install-checklist.md` and the
   cron bodies in `templates/`. The checklist includes the visibility
   tests and how to interpret their FAIL results.

## Status and gate corrections (reference deployment, Oct 21, 2026)

Version 0.1.0 recorded both janitor activation gates as closed. That is
superseded:

- **§11 load replay: waived as a janitor gate** by the deployment's owner
  on Oct 21, 2026. `REPLAY.md` remains an optional validation procedure.
- **Visibility gate: failed and superseded.** The smoke test failed on
  Oct 19, 2026 and twice more on Oct 21, 2026 — a cron session's worker
  listing returned 0 agents while the governor reported 5
  running/admitted, and an agent spawned by a cron session also saw 0,
  not even its parent. The gate's purpose (never over-admit while blind)
  is preserved by the self-contained janitor's conservative rules instead
  of by the test.
- **Janitor: active in self-contained conservative mode.** The
  `governor-janitor` cron (every 10 minutes) reconciles from the queue
  log and /proc evidence, requeues interrupted items under the attempt
  cap, and admits/spawns at most 2 items per tick under the HOLD file,
  the load breaker, slot limits, and an unexplained-compiler guard.
  Workers on tracker-linked items also write checkpoint heartbeats
  into their Work Tracker entries every ~5 minutes; a checkpoint
  younger than 10 minutes protects a running item like a live
  sighting. Workers generate the heartbeat line with the
  `checkpoint-line` command and never hand-write it, an item id, or a
  timestamp (hand-written lines failed three ways on Oct 21, 2026);
  the janitor flags malformed checkpoints in its log instead of
  letting them fail silently. (The `gates.janitor_cron_active` flag in limits.yaml
  lagged activation briefly — the janitor build could not change
  limits values; it was flipped in a separate change and the status
  line now reads ACTIVE.)
- **Interim cap: still in force** for chat dispatch. The cap and the
  governor procedure apply together, stricter limit winning; enabling
  the janitor did not retire it.

Enforcement is conventional throughout: there is no platform scheduler,
so the governor works because every agent follows the same written
procedure against shared state. Violations are visible and attributable
after the fact, not impossible — the design says so plainly, and so does
this README.

## Layout

| File | What it is |
|---|---|
| `SKILL.md` | The agent-facing playbook: hard rules, the interim cap, the governor procedure, HOLD, checkpoints, and summaries of the refill and janitor. |
| `references/` | Full janitor and refill rules (`janitor.md`, `backlog-and-refill.md`) and the incident and gate-status record (`history.md`). |
| `CHANGELOG.md` | Version history. |
| `AGENTS-TEMPLATE.md` | The AGENTS.md sections to adopt, with placeholders. |
| `templates/janitor-cron-body.md` | The janitor cron body as a placeholder template. |
| `templates/refill-cron-body.md` | The refill cron body as a placeholder template. |
| `templates/install-checklist.md` | The combined install checklist (governor → tracker → refill → janitor). |
| `VERSION` | Skill version. |
| `governor.py` | The reference implementation. |
| `test_governor.py` | 81 tests covering budgets, promotion order, aging, fan-out, reservations, the breaker, locking, the janitor snapshot, checkpoint heartbeats, checkpoint-line generation, malformed-checkpoint flagging, the HOLD file, crash-torn log lines, the enqueue brief lint, and the CLI. |
| `brief_lint.py` / `test_brief_lint.py` | Brief lint run by `enqueue`; `--fix` appends the standing artifact instruction. 9 tests. |
| `limits.yaml` | Template limits with tuning guidance. |
| `DESIGN.md` | The design and decision record, including the council-modifications addendum. |
| `REPLAY.md` | The load-replay validation procedure. |
| `examples/example-brief.md` | What a self-contained queue-item brief looks like. |
| `governor-space/` | The Governor Queue space definition: the governor's control surface — live queue board with enqueue, admission, cancel/requeue, HOLD, and close; see `governor-space/README.md` and `governor-space/ACTIONS.md`. |
