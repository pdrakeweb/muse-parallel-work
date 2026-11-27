# Changelog

## 0.2.5

- **Fix:** SKILL.md said `enqueue` runs `brief_lint.py` before any queue write, but `governor.py` never called it. CLI `enqueue` now runs the lint and rejects an in-scope brief that lacks the standing artifact instruction with exit code 4 and `{"error": "brief_lint"}`, before any write. A missing `brief_lint.py` is reported, not skipped.

- **Fix:** a Store loaded its state before taking the lock, so the second of two concurrent CLI writers rewrote `state.json` from a stale copy and dropped the first writer's change. State is now re-read under the lock.
- **Fix:** a process whose lock was stolen (it overran the steal threshold) deleted the thief's live lock when it finally released. Locks now carry a token, release checks it, and a steal renames the directory atomically before removing it.
- **Fix:** a crash mid-append left a torn line with no newline. The next event was glued onto it, and `rebuild` stopped at the first bad line, losing all later history. Appends now start on a fresh line, and `rebuild` skips a bad line instead of stopping.
- **Fix:** `enqueue --fanout N --tracker-ref <id>` dropped the tracker link on the bundle parent.
- **Fix:** `enqueue --fanout N --brief-file <path>` gave the children a placeholder brief instead of the file's contents.
- **Fix:** `admit` ignored the HOLD file, although the docs said every pass checks it. `admit` now admits nothing while HOLD exists (reconciliation still runs) and reports `hold`/`hold_reason`. `status` shows the hold.
- The refill template now passes `--tracker-ref` for Work Tracker items, so refill-enqueued work gets checkpoint protection.
- SKILL.md is restructured: hard rules and the procedure come first, and the deep janitor, refill, and history material moves to `references/`. The frontmatter description is trigger-focused, and the version history lives here.
- Test suite: 81 governor tests plus 9 brief_lint tests.

## 0.2.4

- Adds `brief_lint.py`, which rejects an in-scope brief that lacks the user's standing artifact instruction; `--fix` appends the line verbatim.
- Rewording: "Pete" becomes "the user", and NEEDS PETE becomes NEEDS USER.

## 0.2.3

- Hardens the checkpoint convention after the first real checkpoints were hand-written wrong three ways: wrong item id, marker buried mid-paragraph, and a timestamp one year stale. Each silently voided protection.
- New `checkpoint-line` command generates the heartbeat line, with the id and timestamp machine-derived.
- The parser finds the marker anywhere in the entry text.
- The janitor flags malformed checkpoints (`checkpoint_malformed` in the snapshot diagnostics, `checkpoint-malformed <id>` in its log).
- 72 tests.

## 0.2.2

- `set-tracker-ref` links an existing queued, admitted, or running item to its Work Tracker entry after the fact. Terminal items are refused.
- 55 tests.

## 0.2.1

- Worker checkpoint heartbeats: workers on tracker-linked items write a GOVERNOR-CHECKPOINT line at least every 5 minutes. The janitor honors a checkpoint younger than 10 minutes as liveness evidence.
- 46 tests.

## 0.2.0

- The operating system around the governor: a Work Tracker as the backlog of record, an hourly fill-only refill cron, a self-contained janitor cron that reconciles from queue state and /proc evidence, the admission/control cron inventory, and the HOLD protocol.

## 0.1.0

- Principles, the interim concurrency cap, and the parallelism governor reference implementation.
