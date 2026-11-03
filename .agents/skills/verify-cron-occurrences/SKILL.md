---
name: verify-cron-occurrences
description: "Model-check the cron scheduler's restart-safe dispatch (tick advance, pending_slot stamp, fire claim, crash restore) with TLA+ and TLC. Use when a change touches cron/jobs.py, cron/occurrences.py, cron/missed_runs.py, cron/scheduler_tick.py, or the dispatch and claim code in cron/scheduler.py, or before you claim that a recurring job fires exactly once."
---

# Verify cron occurrences

This skill checks one promise of `cron/AGENTS.md`: every occurrence of a recurring job is accounted for, across ticks, crashes, restarts, and a second ticker on the same store. The check is a TLA+ model that TLC explores exhaustively within fixed bounds. A pass covers the model and its bounds, not the whole scheduler.

## What it verifies

- `AtMostOnce`. One scheduled occurrence of a recurring job never starts two runs. If it breaks, a user gets a duplicate delivery and duplicate side effects.
- `NoSilentLoss`. An occurrence that a tick took off the schedule stays recorded in the job's `pending_slot` until a run claims it or a logged policy skips it. If it breaks, a scheduled run never happens and no log or ledger row says why (#107485).

The scope is built-in recurring (`cron` and `interval`) jobs on the tick path, with two tickers on one store, up to three process deaths, and restarts.

## Run it

```
python .agents/skills/verify-cron-occurrences/scripts/verify.py
```

- It needs Java 11 or newer. The first run downloads TLC `v1.7.4` (`tla2tools.jar`, sha256 pinned in the script) into the user cache. Pass `--tla2tools PATH` or set `TLA2TOOLS_JAR` to supply the jar yourself. The sha256 must match.
- It needs only the Python standard library. Run it with the repo's Python so that the last step can import `cron.jobs`. Without that environment the replay step prints `skipped` and everything else still runs.
- It takes about 20 seconds. Exit code 0 means every result matches `coverage.json`. TLC stops at the first violation, so the state count of a violating run changes from run to run. The `envelope` count does not.
- Narrow it with `--code-only`, `--model-only`, `--only cfg1,cfg2`, or `--no-replay`. `--out DIR` keeps the TLC output where you want it.

Read every TLC result with the `/statechart` skill. It turns a violation into its shortest path of steps and draws the state chart when a run passes. Each run's output is `tlc.out` under the output directory that the script prints.

## Read the result

The script prints a `CODE` block, a `MODEL` table, and a `REPLAY` line.

| Kind | Expected | Meaning of a mismatch |
|---|---|---|
| baseline `envelope` | pass | A change broke the protocol under the stated assumptions. Treat it as a bug until proven otherwise. |
| finding | violates | The code still has the hazard. A pass means the hazard was fixed. Update the expectation and the findings below after you confirm the fix. |
| mutant | violates | The check lost its teeth for that broken step. Fix the model, not the mutant. |
| witness | violates | The model no longer reaches that path, so a pass on `envelope` means less. Fix the model. |

A `CODE` line starting with `DRIFT` means a covered definition changed since the recorded fingerprint. `FACT` means a call order that the model relies on no longer holds, and the line names the mutant that mirrors the break. `MISSING` means a definition moved or vanished. `REPLAY error` means a step of a finding's replay no longer holds on the real code, and the harness error names the step.

To tell a bug from a modeling error, replay the counterexample against the real code, one step per model action. If a step cannot happen, the model is wrong. Fix the model from the code, never from the result. If every step can happen, you found a bug. Report it with the counterexample. Do not weaken a property or a model to make it pass, and do not change product code unless asked.

## Known findings

Both reproduce on the real store functions with `scripts/replay_findings.py` (the `REPLAY` line).

F-A, a second run of one occurrence. Run by `finding_lease_expiry`.

1. The gateway ticker's tick stamps `pending_slot` for slot S1 in `get_due_jobs`, moves `next_run_at` to S2 in `advance_next_runs`, and registers the dispatch in `_submit_with_guard`. The executor is busy, so the worker has not started.
2. `FIRE_CLAIM_TTL_SECONDS` (300 s) pass. The stamp's lease lapses while its owner is alive.
3. A second ticker (`hermes cron tick`, or another gateway on the same store) runs `get_due_jobs`. `unclaimed_pending_slot` sees a foreign, expired stamp and returns S1. `_restore_unclaimed_slot` puts `next_run_at` back on S1, and the second ticker dispatches it.
4. The second ticker's worker wins `claim_job_for_fire`, runs S1, and `mark_job_run` clears the claim.
5. The gateway's queued worker starts. `claim_job_for_fire` finds no live claim and wins. S1 runs again.

It needs a saturated bounded executor (`cron.max_parallel_jobs`) for over 300 s and a second ticker.

F-B, a lost occurrence. Run by `finding_stamp_erased`.

1. Run 1 is in flight. Its `claim_job_for_fire` cleared the stamp and set `fire_claim`.
2. Slot S2 comes due before run 1 ends. The next tick's `get_due_jobs` stamps `pending_slot` for S2, and `advance_next_runs` moves `next_run_at` past S2. The tick has not reached `_submit_with_guard`.
3. Run 1 finishes. `mark_job_run` calls `_record_run_outcome`, which runs `job.pop("pending_slot")` and erases the stamp that belongs to S2.
4. The process dies, or the executor refuses work, before `_submit_with_guard` registers S2.
5. After the restart `next_run_at` is past S2 and no stamp exists. S2 never runs and nothing records it.

It needs a run that overlaps the next slot and a death inside the window between a tick's scan and its submit. The replay reaches the state after step 3. Step 4 is the model's step.

## The check can fail

Each mutant runs under the `envelope` assumptions with one step of the code removed. Each row names the edit to the real code that the mutant mirrors.

| Mutant | Real-code edit | Counterexample |
|---|---|---|
| `mutant_no_stamp` | Drop `pending_slot=pending_slot_stamp(...)` from the `scan.persist(...)` call in `_evaluate_due_job`. | Slots 1 to 3 are due. A tick's scan fast-forwards `next_run_at` to slot 4 with no stamp, and the gateway dies. After the restart nothing is due and nothing ran (`NoSilentLoss`, 7 steps). |
| `mutant_claim_keeps_stamp` | Delete `job.pop("pending_slot", None)` in `claim_job_for_fire`. | The gateway stamps slot 1, queues, claims, and dies mid-run. The second ticker restores the leftover stamp, dispatches slot 1, and claims it. Slot 1 runs twice (`AtMostOnce`, 14 steps). |
| `mutant_no_advance` | Remove the `_advance_or_drop_recurring(...)` call from `_tick_admitted`. | The gateway queues slot 1 without advancing. The next tick sees slot 1 due again and queues a second dispatch. Both claim in turn (`AtMostOnce`, 13 steps). |
| `mutant_restore_ignores_lease` | Remove the live-claim early return from `unclaimed_pending_slot`. | The second ticker restores the gateway's live stamp while the gateway's dispatch is queued. The gateway claims and dies, and the second ticker claims the same slot (`AtMostOnce`, 14 steps). |

The three `witness_*` runs must find a violation. That proves the dead-owner restore, the lost-claim branch, and the already-running refusal are reachable, so the `envelope` pass is not vacuous.

## Model to code

Model: `model/CronOccurrences.tla`. Each action carries the name of the code it mirrors.

| Action | One indivisible step in the code |
|---|---|
| `AcquireTickLock` | `scheduler_tick._acquire_tick_lock_or_degrade` |
| `GetDueJobs` | `jobs.get_due_jobs` under `_jobs_lock`, which runs `_evaluate_due_job`: restore through `_restore_unclaimed_slot`, `fast_forward_missed_recurring`, then the stamp, in one `save_jobs` |
| `AdvanceNextRuns` | `scheduler_tick._advance_or_drop_recurring` calling `jobs.advance_next_runs` |
| `SubmitWithGuard` | `scheduler._submit_with_guard` with `try_register_running_job` |
| `ClaimJobForFire` | `scheduler._process_due_job` calling `jobs.claim_job_for_fire` |
| `MarkJobRun` | `jobs.mark_job_run` with `_record_run_outcome` and `_advance_after_run` |
| `ExpireStamp` | the 300 s lease in `jobs._claim_is_live` and `constants.FIRE_CLAIM_TTL_SECONDS` |
| `Crash`, `Restart`, `AdvanceTime` | environment, not code. A restart gets a new `_machine_id()`. |

Covered files and definitions. `coverage.json` holds the fingerprints and call-order facts for each.

| File | Definitions |
|---|---|
| `cron/scheduler_tick.py` | `_tick_admitted`, `_advance_or_drop_recurring` |
| `cron/jobs.py` | `get_due_jobs`, `_get_due_jobs_locked`, `_evaluate_due_job`, `_restore_unclaimed_slot`, `advance_next_runs`, `claim_job_for_fire`, `mark_job_run`, `_record_run_outcome`, `_advance_after_run`, `_claim_is_live` |
| `cron/occurrences.py` | `unclaimed_pending_slot`, `pending_slot_stamp` |
| `cron/missed_runs.py` | `fast_forward_missed_recurring` |
| `cron/scheduler.py` | `_submit_with_guard`, `try_register_running_job`, `_process_due_job` |
| `cron/constants.py` | `FIRE_CLAIM_TTL_SECONDS` |

Sync commit: 48058f84b39fc81de234f815c2be9a480196933f

## Bounds and assumptions

- Bounds. Four occurrences, one recurring job, two tickers, at most three crashes, three incarnations per ticker. The `envelope` run explores about 280,000 states.
- `envelope` assumes the stamp lease never lapses while its owner's dispatch is queued (`StampLeaseCanExpire = FALSE`). The code lets it lapse, which is F-A.
- `envelope` assumes a run ends before the next slot comes due (`RunsFinishWithinSlot = TRUE`). The code lets runs overlap, which is F-B.
- All runs assume a tick's scan, advance, and submit happen at one clock instant (`TickIsInstant = TRUE`). The real gap is milliseconds to a few seconds.
- A process is dead exactly when it has crashed and not restarted. A claim stays live while its owner process lives. Cross-host owners and an explicit `HERMES_MACHINE_ID` are not modeled.
- A run that claims at time t covers every occurrence due by t. An overlap refusal, a lost claim, and a catch-up-off skip count as logged policy skips.

## Not covered

One-shot jobs. Manual and extra fires (`cron/extra_fire.py`). The grace and catch-up arithmetic in `cron/missed_runs.py`, which is abstracted to "stale or not". The executions ledger and `completed_occurrence`. The 30 s degraded mode of `_jobs_lock`. Unwritable-store paths (`store_health`). External providers and `fire_due`. Profile multiplexing. The executor, the restart-safe external worker, and delivery. The replay drives the store functions only, not the scheduler or its executor.

## When a change touches covered code

1. Run the script before you change anything, then after. A `CODE` line names what moved.
2. Read the diff from the sync commit with `git diff 48058f84b39fc81de234f815c2be9a480196933f -- <file>` for each flagged file. Decide for each change whether it has no effect on the model, whether a model step is no longer one indivisible step in the code, or whether the promise changed on purpose.
3. If the model must change, edit `model/CronOccurrences.tla` from the code, rerun, and judge the result as in "Read the result". Add a mutant for any new step that protects a property.
4. Record the confirmation only after you have checked that the model describes the code at the new commit. Run `python .agents/skills/verify-cron-occurrences/scripts/verify.py --record --sync <commit>`. It rewrites the fingerprints in `coverage.json` and the sync commit line in this file. Never run it to silence a `DRIFT` you did not read.
5. For changes that skipped this skill, use `/maintain-formal-verification-skill`.
