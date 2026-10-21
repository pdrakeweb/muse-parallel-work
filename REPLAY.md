# §11 Replay — Procedure

The validation run from DESIGN.md §11: replay the Oct 18, 2026 load shape under the governor and measure whether the incident recurs. **Status: NOT YET RUN — and, since Oct 21, 2026, no longer an activation gate for the janitor in the reference deployment: the owner waived it as a gate, and the self-contained janitor design does not depend on it.** It remains an optional validation procedure: run it only when the backlog has drained and the user (or the supervisor) calls it. This document is the procedure, not a result.

## Preconditions

- The current batch backlog has finished; `subagent.list` shows no chat-dispatched workers running.
- Normal scheduled background is left running as-is (hourly email triage, camera sweeps, reminders). Do not disable crons to make the test easier — the reservations in `limits.yaml` exist to account for them, and the replay must prove that accounting under a realistic background.
- `governor.py status` shows an empty queue and the gates section reads as expected.
- A stopwatch path for the chat-latency probe: a second chat (or the user's phone) ready to send one ordinary message to the main chat at a chosen moment.

## The load shape (enqueue, do not hand-spawn)

Enqueue the Oct 18 mix through the normal procedure (brief, `governor.py enqueue`, one admission pass, spawn admitted, `started`, close out on handoff):

1. **Seven build items** — feature-implementation-shaped briefs, class P3, resource build. Synthetic stand-ins are acceptable if no real backlog exists, but real work is preferred: the point is real cargo builds and gates, not simulated load.
2. **One review bundle** — a coordinator with `--fanout 11`, class P4, resource light, mirroring the Oct 18 code review.
3. **Docs refresh** — class P4, light.
4. **Competitive analysis** — class P4, light.
5. **One inference-heavy light item** — a diagnostics/research task whose work is dominated by model inference rather than file or network waits (e.g. a diagnostics read over a large evidence bundle), class P2, light. This item matters: the Oct 18 population mixed CPU-heavy builds with inference-heavy agents sharing one inference queue, and a replay with only idle-waiting light workers would understate the contention chat actually felt.
6. Mid-run, when the queue is at its fullest, **enqueue one more P2 light item** (a small diagnostics read) to prove a fresh P2 jumps the P3/P4 backlog without preempting anything running.

## Measurement

- Sample `subagent.list` once a minute for the whole run; record worker count, build count, and the governor's own `status` output alongside.
- Record `/proc/loadavg` at the same cadence.
- At the moment of peak admitted load, send the probe message in chat and time the reply end to end.
- Let the run finish without manual promotion. Note wall-clock start and finish for the whole batch, and when the last bundle child starts (the aging/starvation check).

## Pass criteria (all must hold)

1. No one-minute sample ever shows more than **5 workers** total or more than **1 build**.
2. Load average stays **≤ ~3** throughout (incident: 17.6).
3. The chat probe sent at peak load receives a reply in **under 30 seconds** end to end (incident: minutes, or messages could not be sent at all).
4. **Every item completes** — none lost, none double-admitted, none stuck in `admitted` past the stale-reservation window without being reaped.
5. The P4 bundle finishes via aging alone, with no manual intervention (starvation check).
6. The mid-run P2 item is admitted ahead of older P3/P4 items at the next pass after its enqueue (priority check), and the batch's total completion time is recorded against the unthrottled incident shape for the throughput comparison — criterion 3 outranks this one if they ever conflict.

## Abort conditions

- Load average above 8 sustained for 5 minutes, or any sign of swap/OOM pressure: stop enqueueing, let running work finish (never kill in flight), and record the replay as failed at the point of abort.
- A real P1 event (med reminder, concerning alert) during the replay: the replay yields; note it and restart the measurement window afterward.

## After the run

- Write the results into STATUS.md (samples summary, peak load, probe latency, per-criterion pass/fail).
- If all criteria pass, record the result as validation evidence in STATUS.md. The replay does not gate janitor activation (waived as a gate Oct 21, 2026), and it does not retire the interim cap: enabling mechanisms or retiring the cap is always the user's or the supervisor's explicit call, in a separate task.
- If any criterion fails, the failure, the samples around it, and the suspected cause go into STATUS.md, and `limits.yaml` is the first tuning surface (light slots down is the expected first move).
