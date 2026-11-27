# Governor Queue space

The parallelism governor as a Muse full-stack space: live queue board with
full control. The governor itself stays a file-based Python CLI
(`governor.py`); the space is its control surface, not a second queue.

## Identity

| Field | Value |
|---|---|
| Name | Governor Queue |
| Slug | `governor-queue` |
| Space ID | `79288ed0-2a81-4438-ba78-a69240922124` |
| Kind | TypeScript full-stack space (React client, server actions, privileged host reader) |
| Placement | Sidebar |
| Live since | Oct 2026 (read-only view); full control added Nov 27, 2026 (build v2) |
| Source of truth | `~/workspace/parallelism-governor/` on the host (`state.json`, `queue.jsonl`, `limits.yaml`, `metrics.jsonl`, `briefs/`, `HOLD`) |

## Architecture

| Layer | Role |
|---|---|
| React client | Queue board: slots, running/queued items in admission order, HOLD state, recent finishes. Polls `getSnapshot` every 20 s while open; manual REFRESH button. |
| Server actions | 8 actions (see `ACTIONS.md`). Read path derives the board from the privileged reader; write path shells to `governor.py` — it never edits queue state itself. |
| Privileged host reader | Confined to the governor directory. Reads `state.json`, `limits.yaml`, `metrics.jsonl`, briefs (12,000-char cap), HOLD presence/text, host load average. No other host access. |

## No-second-queue rule

Every mutation goes through `governor.py`'s own lock, brief lint
(`brief_lint.py`), sanity checks, admission rules, and the Store/`do_close`
path. The space keeps no queue state of its own. Admission marks items
admitted; spawning workers stays with the janitor / chat dispatch procedure
(`governor.py admit`, then spawn, then `started`).

## Derived values (observed live, Nov 27, 2026)

| Value | Number | Meaning |
|---|---|---|
| Build budget | 2 | Max concurrent build-resource items |
| Light budget | 9 | Max concurrent light-resource items |
| Total budget | 10 | Max concurrent items overall |
| Urgent budget | 2 | Urgent-lane slots |
| Load breaker | 15.0 | Admission stops above this 1-min load average |
| Build-admission cap | 2 | Build slots minus active urgent reservations |
| Aging | +1 class / 60 min, cap P2 | Queued items promote over time so nothing starves |
| Lock-steal threshold | 5.0 s | Stale-lock takeover window |
| Admission-pass p95 | 0.14 s | Recent passes 0.07–0.17 s |
| Cron reservations | 1 / 3 / 0 | Email triage 1, camera sweep 3, reminders/watchdog 0 |
| Janitor cron | active | `governor-janitor` tick reconciles from queue log + /proc |
| Interim cap | in force | Chat-dispatch cap applies alongside the governor; stricter limit wins |

Budgets come from `limits.yaml`, tuned to the host. P1 work never enters
the queue. HOLD pauses all admissions, urgent work included, until lifted.

## Action → governor mapping

| Space action | Governor path |
|---|---|
| `getSnapshot` | Privileged read of `state.json`, `limits.yaml`, `metrics.jsonl`, HOLD |
| `enqueueItem` | `governor.py enqueue` + `brief_lint.py` (idempotency key, deps, fan-out) |
| `runAdmissionPass` | `governor.py admit` (refused while HOLD is active) |
| `cancelItem` | Store/`do_close` as cancelled, recorded in the event log |
| `requeueItem` | Requeue to queued at original class |
| `setHold` / `clearHold` | Create / remove the HOLD file (with reason text) |
| `closeItem` | `governor.py done` / `fail` (optional error note on failure) |

Full request/response contracts: `ACTIONS.md`.

## Verification (Nov 27, 2026)

| Check | Result |
|---|---|
| Live render | HTTP 200, no console errors, desktop + mobile, 0 px mobile overflow |
| Live data | Real items, not samples: 2 running, 0 queued, 15 done / 24 h, 39 failed all-time |
| Enqueue → admit → cancel → requeue | Proven through the space with two P4 verification items (both cancelled after the check) |
| HOLD set / clear | Part of the v2 full-control edit |
| Polling | 20 s while open + manual refresh |

## Rebuilding from this definition

This directory holds the space's definition, not its built bundle. To
recreate it: build a TypeScript full-stack Muse space from this definition
— React client, server actions per `ACTIONS.md`, privileged reader confined
to the governor directory per the table above — and point it at a live
governor directory. Do not seed sample queue items; the board shows the
governor's real state or nothing.

## Open verification notes

An inspection (Nov 27, 2026, before the v2 edit) found three issues in the
read-only build: dead `entries`-table database scaffold, an unused `recharts`
dependency, a live-dot label claiming "entries save automatically", and a
catalog `construction_status: failed` contradicting `ok:true` build/audit
records. The v2 full-control edit was requested after that inspection;
confirm in the live space that the scaffold, the label, and the catalog
status are resolved before treating them as closed.
