# Example item brief — the file written at enqueue time

> A brief is the self-contained task description stored under `briefs/`
> when an item is enqueued. It contains everything a worker needs, so any
> dispatcher session — not only the chat that originated the request — can
> start the item correctly hours later. This is a worked example with the
> names generalized.

---

# Fix the export-timeout defect in the report service

## Task

The report service's CSV export times out on accounts with more than
~50k rows. Reproduce, find the cause, fix it on branch
`feature/fix-export-timeout`, and run the full gate set.

## Context

- Repo: the service monorepo, base = current main.
- The export path is `crates/report/src/export.rs`; the timeout is the
  gateway's 30 s limit, and the export currently materializes the whole
  result set before streaming.
- A prior investigation (linked in the tracker) ruled out the database
  index as the cause; do not re-litigate that — start from the streaming
  behavior.

## Constraints

- Feature-scoped commits; messages name the fix.
- No behavior change to the export format itself.
- Stay on the existing dependency set; no new crates without flagging it
  in the report.

## Gates (all must pass before the item is done)

- `cargo test --workspace`
- `cargo clippy --workspace --all-targets`
- The repo's UI build, if the change touches the UI crate.

## Preconditions (revalidate at start; fail stale, do not run blind)

- Base main commit matches the one recorded at enqueue, or the diff since
  then does not touch `crates/report/`.
- Branch `feature/fix-export-timeout` does not already exist on the
  remote.

## Checkpoint / resume

Long-running: write progress to `~/workspace/.resume/fix-export-timeout.md`
as you go (what is done, what remains, current gate status). Remove the
marker on completion.

## Report back

To the owner chat: what the cause was, the fix in one paragraph, gate
results with numbers, the branch tip commit, and anything left unverified.
Do not merge; merging is a separate item.
