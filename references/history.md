# History and evidence

<!-- Moved verbatim from SKILL.md in v0.2.4. SKILL.md keeps the operating summary; this file holds the full rules and their evidence. -->


## Why this exists (the Oct 18, 2026 incident)

On Oct 18, 2026, on a 2 vCPU / 7 GB VM, 14+ sub-agents ran concurrently
across chats — feature implementations compiling Rust, a design task, a
docs refresh, a competitive analysis, and a code-review coordinator that
itself fanned out 11 sub-reviewers — plus scheduled scan classifiers.
Measured load average hit **17.6, about 9x oversubscribed**. The user could
not send messages in some chats; replies took minutes.

No single dispatch was wrong. Every chat agent followed the standing rules
— dispatch heavy work, fan out aggressively — in isolation. Nothing
anywhere counted the total, priced the hardware, or ordered competing
requests against each other. Chat, reminder workers, and batch workers
share the same runtime and inference queue, so batch overload directly
degrades interactivity. This system is the counting, pricing, and ordering.

## Deployment location rule — the evidence

On this class of VM those trees are ephemeral and are wiped on replacement.
Proven Oct 19, 2026: a VM event reset `/usr/local` roughly 50 minutes after
a compiler cache (sccache) and a fast linker (mold) were installed there;
every subsequent build invocation failed until both were reinstalled under
`~/bin`. The companion rule for anything you install while adopting this
skill: binaries to `~/bin`, caches to `~/.cache`, config to `~/.config` or
the tool's home-relative config path — never a system tree. The AGENTS.md
template in this skill states the same rule, because the agent that forgets
it will be a future session, not you.

## Known gap: the direct-spawn override — the incident

`governor.py started` refuses an item that is queued but not admitted —
there is no override path. On Oct 19, 2026 the user directed a job to
start immediately anyway (spawned directly, outside a pass); the item
was later closed from queued state via `done`, which does accept it.
Until the tool grows an explicit override, the honest handling is: tell
the user the spawn bypassed admission, spawn only on their explicit
instruction, and close the item out normally when it lands.

## Gate status — corrected Oct 21, 2026

Version 0.1.0 recorded both janitor activation gates as CLOSED. That is
no longer the state of the reference deployment, and the record is
corrected here rather than left standing:

| Gate | Status at v0.2.3 | What changed |
|---|---|---|
| §11 load replay (`REPLAY.md`) | **Waived as a janitor gate** by the deployment's owner, Oct 21, 2026 | The replay remains a useful validation procedure for a quiet host. It is not a prerequisite for the janitor, and it was never run as a gate. |
| Cron-session visibility smoke test | **Failed, and superseded** | Failed Oct 19, 2026 and twice more on Oct 21, 2026 (evidence below). The original janitor design reconciled from the platform's worker listing; the self-contained janitor (Mechanism 4) removes that dependency. The gate's purpose — never over-admit while blind — is preserved by the janitor's conservative rules, not by the test. |
| Interim concurrency cap | **Still in force** for chat dispatch | Enabling the janitor did not retire it. Cap and governor apply together; the stricter limit wins. |
| Janitor | **Active in self-contained conservative mode** in the reference deployment | The `governor-janitor` cron (every 10 min) was created and enabled Oct 21, 2026, with its governor helpers (`janitor-snapshot`, `admit --block-build`, `requeue-interrupted`) implemented and tested in the live workstream. |

A wart that has since closed: `governor.py status` reads its gates line
from the `gates.janitor_cron_active` flag in `limits.yaml`, which the
janitor build was not permitted to change, so the line printed "janitor
cron NOT active" after activation. The flag was flipped to `true` in a
separate permitted change later on Oct 21, 2026, and the gates line now
correctly reads "janitor cron ACTIVE".

