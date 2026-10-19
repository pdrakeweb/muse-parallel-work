# muse-parallel-work

A skill for managing **parallel agent work on a shared machine**: how an
agent system fans work out to sub-agents without saturating the host it is
also trying to talk to its user on.

It has three layers, in adoption order:

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
   `~/workspace/parallelism-governor/`) and copy `governor.py`,
   `test_governor.py`, `limits.yaml`, `DESIGN.md`, and `REPLAY.md` into
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
4. Verify: `python3 -m unittest test_governor` (21 tests) and
   `python3 governor.py status`.
5. Paste the filled-in section from `AGENTS-TEMPLATE.md` into the
   AGENTS.md your agent runtime loads. That template carries the dispatch
   rule, the fan-out default, the interim cap, the governor procedure,
   and the deployment location rule — the instructions future sessions
   will actually follow.

## Gate status (reference deployment)

Both of the governor's activation gates are **closed**, deliberately:

- **Janitor cron: not active.** A janitor would run admission passes on a
  timer so queued work progresses while nobody is chatting. Its gate is a
  visibility smoke test — a cron session must see the same worker
  population the governor reconciles against. The reference deployment
  ran that test on Oct 19, 2026 and it **failed**: a cron session's
  worker listing returned 0 agents while chat-dispatched work was
  verifiably running. A cron session sees only its own session, so
  janitor-based reconciliation would be unsound. **Admission stays with
  chats.**
- **Interim cap: not retired.** The cap and the governor procedure apply
  together, stricter limit winning, until the smoke test passes *and*
  the §11 load replay (`REPLAY.md`) passes, and the retirement is called
  explicitly.

Enforcement is conventional throughout: there is no platform scheduler,
so the governor works because every agent follows the same written
procedure against shared state. Violations are visible and attributable
after the fact, not impossible — the design says so plainly, and so does
this README.

## Layout

| File | What it is |
|---|---|
| `SKILL.md` | The agent-facing playbook: principles, both mechanisms, full governor procedure, gates, known gaps. |
| `AGENTS-TEMPLATE.md` | The AGENTS.md sections to adopt, with two placeholders. |
| `governor.py` | The reference implementation. |
| `test_governor.py` | 21 tests covering budgets, promotion order, aging, fan-out, reservations, the breaker, locking, and the CLI. |
| `limits.yaml` | Template limits with tuning guidance. |
| `DESIGN.md` | The design and decision record, including the council-modifications addendum. |
| `REPLAY.md` | The load-replay validation procedure. |
| `examples/example-brief.md` | What a self-contained queue-item brief looks like. |
