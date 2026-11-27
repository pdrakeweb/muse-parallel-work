# AGENTS.md template — parallel work sections

> TEMPLATE. Paste the sections below into the AGENTS.md your agent runtime
> loads, after filling in the placeholders:
>
> - `{{GOVERNOR_DIR}}` — the governor directory, under the home directory
>   (reference value: `~/workspace/parallelism-governor`).
> - `{{LIMITS_SUMMARY}}` — your tuned budgets in one line (reference value:
>   `build 1, light 4, total 5`).
> - `{{WORK_TRACKER_SLUG}}` — the Work Tracker artifact's Space slug
>   (reference value: `llm-service-manager-work-tracker`).
> - `{{GOVERNOR_GOAL_WORKSPACE}}` — the goal workspace holding the
>   refill/janitor bookkeeping (reference value:
>   `~/workspace/goals/parallelism-governor`).
> - `{{COUNCIL_SKILL_PATH}}` — the council skill the refill uses for
>   ill-defined items (reference value:
>   `~/workspace/custom-skills/council/SKILL.md`).
> - `{{OWNER_GOAL}}` — the owner recorded on the control crons (reference
>   value: `goal:parallelism-governor`).
> - `{{MAIN_CHAT_ID}}` — the chat that receives control-cron reports.
>
> Everything else is written to be adopted verbatim. The wording is the
> operating contract between future sessions; change it deliberately, in
> the open, not by drift.

---

## Dispatch heavy work to subagents; the chat agent stays conversational

The agent in any chat (main or side chat) is a dispatcher and
conversationalist, not the worker, for substantive tasks:

- Coding work — investigation, planning, implementation, debugging,
  release work — is dispatched to a subagent, never executed inside the
  chat agent's own turn.
- Deep research and extended web search follow the same pattern: dispatch
  to a subagent (or the research route), don't research inline.
- On receiving such work, the chat agent replies promptly that the
  instructions were received and dispatched, names what was dispatched in
  one line, then ends its turn and remains available for ongoing
  conversation.
- Results return through the normal handoff; the chat agent reports them
  when they arrive. It does not poll, block, or run long tool chains in
  its own turn while the user waits.
- Quick single-step checks and direct answers (reading one file, a status
  check, a short factual answer) stay in-chat. Dispatch is for work that
  would otherwise occupy the turn.
- How this composes with the Parallelism default below: the chat agent's
  own turn stays short BECAUSE the work fans out. Parallelism is something
  the dispatched subagents do (and a coordinator subagent fans out further
  when a job needs several workers) — it is never the chat agent running
  parallel work inside its own turn. Brief turn, wide fan-out behind it:
  both rules are satisfied by the same act of dispatching.

Why: a chat agent that spends a whole turn executing work appears crashed
— messages are accepted but nothing replies until the turn ends.

## Parallelism default

Default to wide fan-out with parallel subagents on every task where the
work is genuinely independent. Don't wait to be asked, and don't be
conservative about it. "Possible" is the operative word: fan out where
parallelism actually helps, not where it just adds coordination overhead.
Hard constraints still apply — task dependencies (step B needs step A's
output) and API rate limits (more parallel agents against a throttled API
can make it slower, not faster). When a task is partially parallelizable,
parallelize the independent parts aggressively and say so briefly. Per the
dispatch rule above, this fan-out is carried by dispatched subagents
(with a coordinator when several workers are needed) — it does not license
the chat agent to execute the work itself in its own turn; the chat
agent's turn stays brief precisely because the fan-out happens behind the
dispatch. Under the governor below, fan-out is exercised within the
admitted budget: enqueue all the independent pieces; the governor runs as
many at once as the hardware serves well.

## Concurrency cap: chat responsiveness outranks throughput

This cap and the governor procedure apply together — including after the
self-contained janitor is enabled — and where they conflict, the stricter
limit wins:

- At most 4 build-heavy subagents (cargo/npm builds, test gates) run
  concurrently across ALL chats combined. Further build-heavy dispatches
  queue and start as slots free.
- Light work (research, docs, reads, analysis) may run up to 8 concurrent.
- A coordinator's fan-out counts against the same budgets — the
  coordinator is not a loophole.
- Before dispatching into an already-loaded system, check the live worker
  list and count what is actually running.
- Never kill in-flight jobs to enforce the cap retroactively; the cap
  governs new dispatches. Chat responsiveness is a hard constraint, not a
  tradeoff: when load is high, delay the dispatch, never the conversation.

## Parallelism governor

The governor in `{{GOVERNOR_DIR}}` meters all subagent dispatch on this
machine. Its design is DESIGN.md there; limits live only in
`{{GOVERNOR_DIR}}/limits.yaml` ({{LIMITS_SUMMARY}}; cron reservations
declared in the same file). Operating procedure for every dispatch, in
every chat:

- Enqueue before you spawn: write the item's self-contained brief under
  `{{GOVERNOR_DIR}}/briefs/`, then run
  `python3 {{GOVERNOR_DIR}}/governor.py enqueue --title "..." --class P2|P3|P4 --resource build|light --owner-chat-id <id> --brief-file <path>`
  (add `--idempotency-key` for work with a natural key, e.g. a branch
  name; `--fanout N` for a coordinator bundle; `--depends-on <id>` for
  ordering). Then run one admission pass: take the current worker snapshot
  from the sub-agent listing, save it as JSON
  (`{"agents":[{"agent_id":..., "status":...}], "cron_active":[...]}`),
  and run `governor.py admit --snapshot <file>`. Spawn only the items the
  pass admits; after each spawn record
  `governor.py started --id <id> --worker <agent_id>`; when the handoff
  lands, close the item with `governor.py done --id <id>` or
  `governor.py fail --id <id>`.
- Classes: P2 = the user is actively waiting on it; P3 = default batch
  work; P4 = background that can slip. Promotion order is (effective
  class, base class, enqueue time); aging promotes +1 class per 60
  minutes queued, capped at P2. The user's urgency words reclassify at
  any time.
- P1 bypass: reminders, alerts, due triage, and anything the user marks
  urgent do NOT enter the queue. They run immediately, they pause new
  admissions until they finish, and they never preempt running work.
- Coordinators enqueue fan-out bundles (`--fanout N`); every child
  consumes its own slot. A coordinator is not a loophole around the
  budgets.
- Circuit breaker: admission passes admit nothing while the 1-minute
  load average exceeds the threshold in limits.yaml.
- Queue hold: a file at `{{GOVERNOR_DIR}}/HOLD` pauses all admissions
  while it exists — every admission pass checks for it first and admits
  nothing while it is present. The holder writes its reason and start
  time inside the file and removes it when done. A HOLD older than 3
  hours is stale: any chat may remove it after noting the fact in its
  reply.
- Worker checkpointing: if the governor item you are running has a
  `tracker_ref` (its Work Tracker entry id — the enqueueing chat records
  it with `enqueue --tracker-ref <id>`, it is set at spawn with
  `started --tracker-ref <id>`, or retrofitted with
  `set-tracker-ref --id <id> --tracker-ref <id>`), checkpoint at least
  every 5 minutes while you work. Generate the checkpoint line with
  `python3 {{GOVERNOR_DIR}}/governor.py checkpoint-line --id <your governor item id> --state "<one-line state>"`
  and append the printed line verbatim as the last line of the entry's
  `details`: read the entry on the Work Tracker, remove any previous
  checkpoint line, preserve the rest of the text, and write it back
  with the update action. NEVER hand-write the checkpoint line, an
  item id, or a timestamp — all three were gotten wrong in the
  reference deployment on Oct 21, 2026 (the original item's id instead
  of the running item's, the marker buried mid-paragraph, a timestamp
  exactly one year stale), each silently voiding protection;
  `checkpoint-line` derives the id from the item's own record and the
  timestamp from the system clock, and refuses unknown, unlinked, and
  terminal items. The janitor reads these back every tick; a
  checkpoint younger than 10 minutes is liveness evidence equal to a
  live worker sighting, and a linked entry whose marker yields no
  valid checkpoint is flagged in the janitor's log as
  `checkpoint-malformed <id>` instead of failing silently. Without
  checkpoints, a run that spends ~60+ minutes in a non-compiling phase
  can be reaped as stale. Items with no tracker link get no checkpoint
  protection.
- Gate status (corrected Oct 21, 2026): the janitor cron
  (`governor-janitor`, every 10 min) runs in self-contained conservative
  mode — it reconciles from the queue log plus /proc evidence, never the
  sub-agent listing (cron-lineage sessions verifiably see zero agents:
  visibility tests failed Oct 19 and twice on Oct 21, 2026), requeues
  interrupted items under the attempt cap, and admits/spawns at most 2
  items per tick under the HOLD file, the limits.yaml load breaker, slot
  limits, and an unexplained-compiler guard on build admissions. The
  DESIGN.md §11 replay is NOT a janitor gate (waived as a gate by the
  owner, Oct 21, 2026); the visibility gate is superseded by the
  self-contained design. The interim cap above remains in force for chat
  dispatch; the cap and this procedure apply together, and where they
  conflict, the stricter limit wins.
- Check state any time with `python3 {{GOVERNOR_DIR}}/governor.py status`.

## Backlog, refill, and control crons

- Backlog of record: the Work Tracker (Space slug
  `{{WORK_TRACKER_SLUG}}`) holds pending work; the governor queue holds
  work ready to run. Tracker projects are General (the default) and
  LLMdil, as a multi-select filter; statuses are Requested / Pending /
  In Progress / Blocked / Done / Future; workstreams are a field and a
  filter. Do not invent projects, statuses, or workstreams — read the
  enumerations from the tracker.
- Refill: the `governor-queue-refill` cron (hourly, owner
  `{{OWNER_GOAL}}`) scans the trackers only when the governor is fully
  idle (no HOLD, 0 running/admitted, 0 queued, load below the breaker),
  classifies items EXECUTABLE / NEEDS USER / ILL-DEFINED, sends
  ill-defined items to a council (a refined brief at ≥ MEDIUM confidence
  may be enqueued), and enqueues at most 5 items per run under stable
  idempotency keys (`refill-<source>-<tracker item id or slug>`). It
  FILLS ONLY — it never runs an admission pass and never spawns, even
  with the janitor active. Bookkeeping:
  `{{GOVERNOR_GOAL_WORKSPACE}}/hidden_files/refill-log.md` and
  `refill-state.json`. Council procedure: `{{COUNCIL_SKILL_PATH}}`.
- Janitor: the `governor-janitor` cron (every 10 min, owner
  `{{OWNER_GOAL}}`) owns the workers it spawns end-to-end: it closes
  them out on handoff before anything else, reconciles stale entries
  from queue state + /proc under the safety rails (never reap a build
  item while any compiler process lives; never reap a light item under
  60 minutes old; ambiguity admits less), admits from a state-derived
  snapshot, and spawns at most 2 items per tick. It never enqueues work
  and never edits the queue files by hand. Supervised mode: action ticks
  report to the main chat (`{{MAIN_CHAT_ID}}`); no-action ticks stay
  silent; every tick appends one line to
  `{{GOVERNOR_GOAL_WORKSPACE}}/hidden_files/janitor-log.md`.
- Other control cron: `weekly-binary-updates` (Sundays ~03:00 local)
  maintains the toolchain binaries under ~/bin and the Python
  environments under the HOLD protocol — it writes its HOLD, waits for a
  compiler-quiet window, and removes only its own HOLD. It is not an
  admission authority.
- HOLD protocol: the holder writes its reason and start time inside
  `{{GOVERNOR_DIR}}/HOLD` and removes only its own HOLD when done. A
  HOLD older than 3 hours is stale: any chat may remove it after noting
  the fact in its reply; a job that finds a HOLD under 3 hours old
  leaves it in place (the weekly job aborts).

## Deployment location rule

Every mechanism in this system lives entirely under the home directory:
governor files and queue state, toolchain binaries, caches, and config.
Nothing is installed under `/usr`, `/usr/local`, or `/etc` — those trees
are ephemeral on this class of VM and are wiped on replacement. (Proven
once the hard way: a VM event reset `/usr/local` under an hour after a
build cache and linker were installed there, breaking every build until
both were reinstalled under `~/bin`.) Toolchain binaries go in `~/bin`,
caches in `~/.cache`, config in home-relative config paths.

## Setup notes (governor)

- `queue.jsonl` is the source of truth: an append-only event log holding
  each item's full record. `state.json` is a materialized view, rewritten
  atomically on every mutation (previous copy at `state.prev.json`); if
  it is ever torn or suspect, regenerate it with
  `python3 {{GOVERNOR_DIR}}/governor.py rebuild` — never hand-edit either
  file.
- An item's life: `queued → admitted → running → done/failed`, with
  `interrupted` for workers that vanish (auto-requeue if resumable, max
  2 attempts) and stale reservations reaped back to `queued`.
- To adapt the numbers to different hardware: measure the host (vCPUs,
  RAM total and available under load, one real build's peak RSS), set
  build slots to the smaller of cores−1 and what the RAM envelope fits,
  light slots from spare RAM, total as their sum, breaker at ~2.5x
  vCPUs — then verify with the test suite in the skill
  (`python3 -m unittest test_governor`) before trusting the queue.
