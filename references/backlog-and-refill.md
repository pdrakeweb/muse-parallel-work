# The backlog: the Work Tracker and the refill cron — full reference

<!-- Moved verbatim from SKILL.md in v0.2.5. SKILL.md keeps the operating summary; this file holds the full rules and their evidence. -->

The governor queue coordinates work that is ready to run. It is not a
backlog: items enter it when someone (or some cron) has decided the work
should be done soon. The backlog of record lives one layer up, in a
**Work Tracker**, and an hourly **refill cron** feeds executable backlog
items into the queue.

## The Work Tracker

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

## The refill cron (`governor-queue-refill`, hourly)

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
   - **NEEDS USER** — the next step is a decision, approval, purchase,
     booking, send, deletion, or policy choice belonging to the owner.
     Never enqueued, and not council material. Work that spends money,
     books, sends messages, deletes data, or changes an account is
     NEEDS USER by definition.
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
   rules. A user-decision ruling, or confidence below MEDIUM, is
   recorded as a `needs-user` disposition with the exact decision
   question, and nothing is enqueued.
6. **Report** to the main chat only when the run did something: enqueued
   items (title, class, resource, source tracker, governor item id) and
   Needs-User items with the exact pending decision. Each Needs-User
   item is reported once; it is not re-reported unless 7 days have
   passed or its tracker entry changed. Not-idle runs, empty scans, and
   fully-dispositioned scans stay silent.
