# Install checklist — the parallel-work operating system

> The combined install sequence for all four components, in dependency
> order: **governor → Work Tracker → refill → janitor**. Do not reorder:
> the refill enqueues into the governor, and the janitor admits and spawns
> what the refill and the chats enqueue. Placeholders match the other
> templates in this pack (`templates/janitor-cron-body.md`,
> `templates/refill-cron-body.md`, `AGENTS-TEMPLATE.md`).

## 0. Prerequisites

- [ ] Host measured: vCPUs, total RAM, RAM available under typical load,
      and one real build's peak RSS and duration for the largest project.
- [ ] A skills directory and a governor directory chosen, **both under the
      home directory**. Nothing in this system is installed under `/usr`,
      `/usr/local`, or `/etc`.
- [ ] The deployment's owner chat id, owner goal, and timezone recorded —
      the cron templates need them.

## 1. Governor

- [ ] Copy this skill folder into the skills directory.
- [ ] Create `{{GOVERNOR_DIR}}` and copy into it: `governor.py`,
      `test_governor.py`, `limits.yaml`, `DESIGN.md`, `REPLAY.md`.
- [ ] Tune `limits.yaml` from the measurements (build slots = the smaller
      of cores−1 and the RAM envelope; light slots from spare RAM; total =
      build + light as the starting point; breaker ≈ 2.5x vCPUs). The
      shipped numbers are the reference host's, not defaults to copy.
- [ ] Verify: `cd {{GOVERNOR_DIR}} && python3 -m unittest test_governor`
      (55 tests) and `python3 governor.py status`.
- [ ] Fill in `AGENTS-TEMPLATE.md` and paste it into the AGENTS.md the
      runtime loads. Operate chat dispatch under the interim cap +
      governor procedure from this point on; the cap stays in force for
      chat dispatch through every later step.
- [ ] Smoke-test the procedure by hand once: write a brief, enqueue,
      snapshot, admit, spawn, `started`, `done`. Confirm `status` reflects
      each transition.

## 2. Work Tracker

- [ ] Create or identify the Work Tracker artifact (reference Space slug:
      `llm-service-manager-work-tracker`). It is the backlog of record:
      the governor queue coordinates execution, the tracker holds the
      backlog.
- [ ] Confirm the record schema: `id`, `title`, `project`, `workstream`,
      `status`, `details`, `created_at`, `updated_at`.
- [ ] Confirm the project enumeration is exactly **General** and
      **LLMdil**; new items default to General, and the project tabs
      default to General selected. Project tabs are a multi-select filter.
- [ ] Confirm the status enumeration is exactly **Requested / Pending /
      In Progress / Blocked / Done / Future**, in that board order.
- [ ] Confirm the workstream enumeration: Releases, Core and Gateway,
      UI and Design, Windows Services, MCP Capabilities, Admin API,
      Android App, Optimize, Prometheus Hardware, Backlog, General.
      Workstream is a filter and a field, not a separate backlog.
- [ ] Confirm the actions work: list items (returning the items plus the
      project/workstream/status enumerations), create item, update item,
      delete item. Create one throwaway item, move it across two statuses,
      and delete it.

## 3. Refill cron (`governor-queue-refill`)

- [ ] Prerequisite: governor working (step 1) and tracker readable through
      its actions (step 2). The refill never admits and never spawns, so
      it is safe to enable before the janitor exists.
- [ ] Create the cron from `templates/refill-cron-body.md`, placeholders
      filled, cadence `{{REFILL_CADENCE}}` (reference: hourly).
- [ ] Run one supervised refill pass with the governor idle. Verify: the
      idle gate was evaluated, candidates were classified
      EXECUTABLE / NEEDS PETE / ILL-DEFINED, enqueues (if any) carry
      idempotency keys of the form `refill-<source>-<tracker item id or
      slug>`, and the log + dispositions files were written under
      `{{GOVERNOR_GOAL_WORKSPACE}}/hidden_files/`.
- [ ] Run it a second time against unchanged trackers. Verify it enqueues
      nothing twice (idempotency keys + dispositions) and stays silent.
- [ ] Verify the boundary: the refill performs no admission pass and
      spawns nothing, even when the janitor is already enabled.

## 4. Visibility tests (run before choosing a janitor design)

The original janitor design reconciled from the platform's worker
listing. Test whether a cron lineage can see the worker population before
relying on that:

- [ ] While chat-dispatched work is verifiably running, run a one-shot
      cron whose only job is to call the sub-agent listing in both scopes
      (direct and descendants) and record the counts next to
      `governor.py status` at the same minute.
- [ ] Run the spawn variant: a one-shot cron spawns exactly one subagent;
      the subagent calls the same listing in both scopes and records what
      it can see (other chats' agents, its parent, itself).
- [ ] File both results as evidence in the governor workstream.

How to interpret the results:

- **PASS** (the cron lineage sees the same population the governor
  reports): a visibility-based janitor is *permitted* to be considered.
  Adopting it is still an explicit decision, and the conservative rules
  remain the safer default.
- **FAIL** (zero agents returned while the governor shows running/admitted
  work — the reference deployment's result on Oct 19, 2026 and twice on
  Oct 21, 2026): the visibility-based design is blocked, permanently for
  that platform behavior. It does **not** block the janitor itself:
  proceed with the self-contained design in step 5, which never reads the
  listing. Record the FAIL as the reason for the design choice.
- **Spawn-variant FAIL** (the spawned agent sees nothing, not even its
  parent): spawning does not escape cron-lineage blindness. The janitor
  must own its workers end-to-end and learn about completions from
  handoffs into its own lineage, never from the listing.
- **Partial or inconsistent visibility**: treat as FAIL. Ambiguity
  resolves to admit less.

## 5. Janitor (`governor-janitor`)

- [ ] Prerequisite: the governor build provides `janitor-snapshot`
      (with `--checkpoints`), `admit --block-build`,
      `requeue-interrupted`, and `extract-checkpoints`, plus the
      `tracker_ref` item field, covered by tests, with the pre-existing
      suite still green. (The governor.py shipped in this skill at
      v0.2.2 includes these helpers; the reference deployment added
      them Oct 21, 2026.) Do not enable the janitor without them.
- [ ] Confirm the gate corrections are recorded in the deployment's status
      notes: the §11 replay is **not** a janitor gate (waived by the owner
      in the reference deployment, Oct 21, 2026); the visibility gate
      failed and is superseded by the self-contained design; the interim
      cap remains in force for chat dispatch.
- [ ] Create the cron from `templates/janitor-cron-body.md`, placeholders
      filled, cadence `{{JANITOR_CADENCE}}` (reference: every 10 minutes).
- [ ] Run one supervised first tick and read its log line. Verify: HOLD
      and load rails were evaluated, reconciliation used queue state +
      /proc only, at most 2 spawns occurred, every spawn was followed by a
      `started` record, and no item with a missing brief was spawned.
- [ ] Let one janitor-spawned worker complete. Verify the completion
      returns as a janitor turn and the janitor closes the item
      (`done`/`fail`) before anything else on that turn.
- [ ] Seed or wait for one stale-entry case (an admitted reservation never
      started, or an interrupted item) and verify the janitor requeues it
      under the attempt cap instead of reaping it outright — and that a
      build item is never reaped while compiler processes are alive, and
      a light item never before 60 minutes.
- [ ] Leave supervised-mode reporting on (action ticks report to the main
      chat; no-action ticks silent) until the owner changes it.

## 6. HOLD protocol check

- [ ] Confirm every operator and cron body states the protocol: the
      holder writes its reason and start time inside
      `{{GOVERNOR_DIR}}/HOLD`; all admission passes admit nothing while it
      exists; the janitor stops its scheduled tick; the holder removes
      only its own HOLD when done; a HOLD older than 3 hours is stale and
      may be removed after noting the fact.
- [ ] Confirm `weekly-binary-updates` (if adopted) follows it: it aborts
      if an existing HOLD is under 3 hours old, notes and replaces a
      stale one, and removes only the HOLD it wrote.

## 7. Rollback

- [ ] Disable `governor-janitor`: the queue stops advancing on its own;
      chats resume opportunistic admission. No state is lost — the queue
      log is the source of truth.
- [ ] Disable `governor-queue-refill`: the backlog stays in the tracker;
      nothing is enqueued automatically.
- [ ] Never roll back by editing `queue.jsonl` or `state.json` by hand;
      rebuild state from the log (`governor.py rebuild`) if it is suspect.
