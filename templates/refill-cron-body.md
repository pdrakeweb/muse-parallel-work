# Template — governor-queue-refill cron body

> TEMPLATE. Create an hourly cron from this body after filling in the
> placeholders:
>
> - `{{REFILL_CADENCE}}` — scan interval (reference value: `1h`).
> - `{{TIMEZONE}}` — the deployment timezone (reference: `America/New_York`).
> - `{{GOVERNOR_DIR}}` — the governor directory, under home
>   (reference: `~/workspace/parallelism-governor`).
> - `{{GOVERNOR_GOAL_WORKSPACE}}` — the goal/workspace directory that holds
>   this system's bookkeeping (reference:
>   `~/workspace/goals/parallelism-governor`).
> - `{{OWNER_GOAL}}` — the cron's owner (reference: `goal:parallelism-governor`).
> - `{{MAIN_CHAT_ID}}` — the chat that receives reports; also recorded as
>   the owner chat of items the refill enqueues.
> - `{{WORK_TRACKER_SLUG}}` — the Work Tracker artifact's Space slug
>   (reference: `llm-service-manager-work-tracker`).
> - `{{COUNCIL_SKILL_PATH}}` — path to the council skill used to refine
>   ill-defined items (reference: `~/workspace/custom-skills/council/SKILL.md`).

---

```yaml
---
id: governor-queue-refill
title: Governor queue refill — hourly tracker scan
enabled: true
owner: {{OWNER_GOAL}}
mode: task
schedule:
  kind: interval
  timezone: {{TIMEZONE}}
  every: {{REFILL_CADENCE}}
timeout_secs: 3600
delivery:
  - chat_id: {{MAIN_CHAT_ID}}
---
```

Hourly governor queue refill. Purpose: when the parallelism governor is idle with no pending work, find pending executable work in the work trackers and add it to the governor queue. This job FILLS the queue only — it never runs an admission pass and never spawns workers. That boundary is a design rule, not a workaround: admission and spawning belong to chats and to the governor janitor, never to the refill.

Bookkeeping files (read/write every run):
- Log: `{{GOVERNOR_GOAL_WORKSPACE}}/hidden_files/refill-log.md` — append one line per run: timestamp, idle or not, items found, items enqueued, councils run, decisions surfaced.
- Dispositions: `{{GOVERNOR_GOAL_WORKSPACE}}/hidden_files/refill-state.json` — map of item key -> {disposition, date, note}. Item key = `refill-<source>-<tracker item id or slug>`. Consult it before acting on any item and update it for every item you disposition.

STEP 1 — Idle gate. All four must hold, else end the run silently after logging:
- No HOLD file at `{{GOVERNOR_DIR}}/HOLD`.
- `python3 {{GOVERNOR_DIR}}/governor.py status` shows 0 running and 0 admitted workers.
- The governor queue is empty (0 queued) — pending work means the system is not idle for this job's purpose.
- load1 is below the circuit-breaker threshold configured in `{{GOVERNOR_DIR}}/limits.yaml` (read it from the status output; do not hardcode a number — the threshold is tuned per deployment and changes over time).

STEP 2 — Scan every work tracker for pending items:
- The Work Tracker (Space slug `{{WORK_TRACKER_SLUG}}`) is the primary source and the backlog of record. Read its open items through its published actions or inspection (items in a Done status are complete; everything else is a candidate subject to the skip rules below).
- Web artifacts: list the artifact catalog (page through all results) and identify any other artifacts that function as work trackers (their name, slug, or records hold work items with pending/open states). Read each tracker artifact's pending items through its published actions or inspection.
- Runtime tracked items and goals: list tracked items; take those still open whose state implies remaining work (pending, user_action_required, new_information with an action implied). For any candidate item, read its goal workspace GOAL.md (`~/workspace/goals/<goal-slug>/GOAL.md`) for the detail behind it.
- Skip items that are completed, cancelled, blocked on an external party with no action available, or explicitly waiting on the owner in the tracker itself.

STEP 3 — Classify each pending item:
- EXECUTABLE: a self-contained worker brief can be written from the tracker entry plus its linked detail (goal doc, artifact record) without inventing any decision the owner has not made. It has a concrete outcome, an identifiable scope and workspace, and a checkable completion condition.
- NEEDS USER (not council material, never enqueued): the item's next step is a decision, approval, purchase, booking, send, deletion, or policy choice that is the owner's to make.
- ILL-DEFINED: pending and plausibly executable, but too underspecified to brief (missing scope, outcome, or completion condition) and not blocked on an owner decision. These go to council in Step 5.

STEP 4 — Enqueue executable items (at most 5 per run):
- Before enqueueing, check the governor state (governor.py status and state.json) for a live or recently finished item covering the same work — same item key, same tracker id, or a near-identical title. Skip duplicates; the governor is the coordination point between chats, and another chat may already own the item.
- Also skip any item whose disposition in refill-state.json is already enqueued.
- For each item: write a self-contained brief to `{{GOVERNOR_DIR}}/briefs/refill-<item-slug>.md` (outcome, scope, source tracker reference, completion check, constraints from the detail docs), then run:
  `python3 {{GOVERNOR_DIR}}/governor.py enqueue --title "<item title>" --class P3 --resource <build if the work compiles or test-gates code, else light> --owner-chat-id {{MAIN_CHAT_ID}} --owner-chat-name "governor-queue-refill" --brief-file <brief path> --idempotency-key <item key>`
  Use --class P2 instead of P3 only when the tracker marks the item urgent or a deadline falls within 48 hours.
- Record each enqueue in refill-state.json with the governor item id returned.

STEP 5 — Council refinement for ill-defined items (at most 2 councils per run):
- Skip items already sent to a council in the last 7 days per refill-state.json; defer the rest to later idle runs.
- Run each council using the council skill (read `{{COUNCIL_SKILL_PATH}}` first): classic panel, 2 rounds. The council brief carries the tracker item verbatim, all context found in its detail docs, and one question: define this work item into an executable brief (outcome, scope, completion check), or rule that an owner decision is required and state exactly which decision.
- If the council produces an executable brief with at least MEDIUM confidence: write the brief and enqueue it as in Step 4 (counts toward the 5-per-run cap), and record the disposition.
- If the council rules an owner decision is required, or confidence is below MEDIUM: do not enqueue. Record disposition needs-user with the council's stated decision question.

STEP 6 — Report to the main chat ONLY when this run did something:
- Items enqueued: title, class, resource, source tracker, and the governor item id.
- Needs-User items (from Step 3 classification or Step 5 councils): the item and the exact decision it waits on. Report each needs-user item once; do not re-report it on later runs unless 7 days have passed or its tracker entry changed.
- Stay silent when the system was not idle, when no pending items were found, or when everything found was already dispositioned.

Hard limits: never enqueue work that spends money, books, sends messages, deletes data, or changes an account — those are needs-user by definition. Never modify governor.py, limits.yaml, or the live queue beyond governor.py enqueue calls. Never edit tracker artifacts; read them only.
