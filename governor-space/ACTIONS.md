# Governor Queue space — action contracts

Live action schemas, captured from the space Nov 27, 2026. All 8 actions are
published on the `governor-queue` space. Reads derive from the privileged
reader; writes go through `governor.py` (see `README.md` — no second queue).

## Actions

| Action | Mutates | Purpose |
|---|---|---|
| `getSnapshot` | No | Slot usage, running and queued work in admission order, HOLD state, recent finishes |
| `enqueueItem` | Yes | Enqueue work with title, class, resource, self-contained brief; optional urgent, constrained, fan-out |
| `runAdmissionPass` | Yes | One admission pass under the governor's own rules; refused while HOLD is active |
| `cancelItem` | Yes | Cancel a queued, admitted, or running item; closed as cancelled in the event log |
| `requeueItem` | Yes | Requeue a failed, cancelled, interrupted, or done item at its original class |
| `setHold` | Yes | Set HOLD with a reason; pauses all admissions including urgent |
| `clearHold` | Yes | Lift HOLD so admission passes can admit again |
| `closeItem` | Yes | Close an item as done or failed, with an optional error note |

## Request schemas

### `getSnapshot`

```json
{}
```

### `enqueueItem`

```json
{
  "title": "string, 1-200 chars",
  "cls": "P2 | P3 | P4",
  "resource": "build | light",
  "briefText": "string, 40-12000 chars",
  "ownerLabel": "string <= 80 chars | null",
  "urgent": "broken-build | user-urgent | security | human-waiting | null",
  "constrained": "boolean",
  "dependsOnText": "string <= 500 chars",
  "idempotencyKey": "string <= 120 chars | null",
  "fanout": "integer, 1-8"
}
```

All fields required. `briefText` must pass `brief_lint.py` (standing
artifact rule, self-contained brief). `urgent` values are the only
urgent-lane reasons: broken-build fixes, user-marked urgent work, security
work, work a human is waiting on. P1 never enters the queue.

### `runAdmissionPass`

```json
{}
```

### `cancelItem`

```json
{
  "itemId": "string, 1-200 chars",
  "reason": "string <= 500 chars | null"
}
```

Both fields required.

### `requeueItem`

```json
{
  "itemId": "string, 1-200 chars"
}
```

### `setHold`

```json
{
  "holdText": "string, 1-500 chars"
}
```

### `clearHold`

```json
{}
```

### `closeItem`

```json
{
  "itemId": "string, 1-200 chars",
  "outcome": "done | failed",
  "errorText": "string <= 500 chars | null"
}
```

All fields required.

## Snapshot shape (`getSnapshot` response)

| Field | Content |
|---|---|
| `usage` | `{ build, light, total, urgent }` running counts |
| `budgets` | `{ build: 2, light: 9, total: 10, urgent: 2 }` (from `limits.yaml`) |
| `active` | Running items in admission order (id, title, class, effective class, resource, attempts, owner, brief text, worker, timestamps) |
| `queued` | Queued items in admission order, same shape |
| `recentDone` / `recentFailed` | Recently finished items, same shape |
| `recentPasses` | Admission passes (`ts`, `admitted`, `durationS`) |
| `holdActive` / `holdText` | HOLD state |
| `load1` | Host 1-min load average |
| `breakerThreshold` / `breakerTripped` | Load breaker (15.0) and state |
| `buildAdmissionCap` | Build slots minus active urgent reservations |
| `agingCapClass` / `promoteAfterMinutes` | Aging rule (cap P2, +1 class / 60 min) |
| `lockStealThresholdS` | Stale-lock window (5.0 s) |
| `passP95S` | Admission-pass p95 duration |
| `cronReservations` | `{ emailTriage: 1, cameraSweep: 3, remindersWatchdog: 0 }` |
| `doneLast24h` / `failedAllTime` | Counters |
| `janitorCronActive` / `interimCapInForce` | Janitor and interim-cap flags |
| `urgentQueued` | Urgent-lane items waiting |
| `stateUpdatedAt` / `fetchedAtMs` | Freshness timestamps |
