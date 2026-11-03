import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, UTC
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))
SCRATCH_HOME = tempfile.mkdtemp(prefix="replay_findings_")
os.environ["HERMES_HOME"] = SCRATCH_HOME

from cron import constants, jobs
import cron.extra_fire
import cron.missed_runs
import cron.occurrences
import cron.quota_hold
import cron.unreachable_retry
import hermes_time


EXIT_REPRODUCED = 0
EXIT_HARNESS_ERROR = 2
EXIT_NOT_REPRODUCED = 3


class HarnessError(Exception):
    pass


class Clock:
    def __init__(self, start):
        self.value = start

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value = self.value + timedelta(seconds=seconds)


class Processes:
    def __init__(self):
        self.running_owner = None

    def select(self, machine):
        os.environ["HERMES_MACHINE_ID"] = machine

    def liveness(self, job_id):
        return self.running_owner is not None and self.running_owner == os.environ.get("HERMES_MACHINE_ID")


def install(clock, procs):
    hermes_time.now = clock.now
    for name, module in list(sys.modules.items()):
        if name == "cron" or name.startswith("cron."):
            if hasattr(module, "_hermes_now"):
                module._hermes_now = clock.now
    jobs._job_running_in_this_process = procs.liveness


def stored(job_id):
    for record in jobs.load_jobs():
        if record["id"] == job_id:
            return record
    raise HarnessError(f"job {job_id} missing from store")


def view(job_id):
    record = stored(job_id)
    pending = record.get("pending_slot")
    claim = record.get("fire_claim")
    return {
        "next_run_at": record.get("next_run_at"),
        "pending_slot": pending.get("scheduled_at") if isinstance(pending, dict) else None,
        "pending_by": pending.get("by") if isinstance(pending, dict) else None,
        "fire_claim_by": claim.get("by") if isinstance(claim, dict) else None,
    }


def note(evidence, label, job_id):
    snap = view(job_id)
    evidence.append(
        f"{label}: next_run_at={snap['next_run_at']} pending_slot={snap['pending_slot']}"
        f" pending_by={snap['pending_by']} fire_claim_by={snap['fire_claim_by']}"
    )
    return snap


def require(condition, message):
    if not condition:
        raise HarnessError(message)


def due_snapshot(job_id, evidence, label):
    due = [j for j in jobs.get_due_jobs() if j["id"] == job_id]
    require(len(due) == 1, f"{label}: get_due_jobs returned {len(due)} snapshots for the job, expected 1")
    evidence.append(f"{label}: due snapshot _scheduled_instant={due[0].get('_scheduled_instant')}")
    return due[0]


def new_job(clock, procs):
    procs.select("A")
    job = jobs.create_job(prompt="replay", schedule="every 5m", name="replay-job")
    return job["id"], stored(job["id"])["next_run_at"]


def scenario_fb(root):
    evidence = []
    clock = Clock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    procs = Processes()
    with jobs.use_cron_store(root):
        install(clock, procs)
        job_id, s1 = new_job(clock, procs)
        s1_dt = datetime.fromisoformat(s1)
        evidence.append(f"S1={s1}")
        procs.select("A")

        clock.value = s1_dt + timedelta(seconds=10)
        due_snapshot(job_id, evidence, "step1")
        snap = note(evidence, "step1 stored", job_id)
        require(snap["pending_slot"] == s1, f"step1: pending_slot {snap['pending_slot']} != S1 {s1}")

        require(jobs.advance_next_runs([job_id]) == 1, "step2: advance_next_runs did not advance")
        snap = note(evidence, "step2 stored", job_id)
        s2 = snap["next_run_at"]
        require(s2 != s1 and datetime.fromisoformat(s2) > s1_dt, f"step2: next_run_at {s2} not after S1")

        claimed = jobs.claim_job_for_fire(job_id)
        require(bool(claimed), "step3: claim_job_for_fire returned falsy")
        snap = note(evidence, "step3 stored", job_id)
        require(snap["pending_slot"] is None, f"step3: pending_slot still {snap['pending_slot']}")
        require(snap["fire_claim_by"] is not None, "step3: fire_claim not set")
        procs.running_owner = "A"

        clock.value = datetime.fromisoformat(s2) + timedelta(seconds=10)
        due_snapshot(job_id, evidence, "step4")
        snap = note(evidence, "step4 stored", job_id)
        require(snap["pending_slot"] == s2, f"step4: pending_slot {snap['pending_slot']} != S2 {s2}")
        require(snap["fire_claim_by"] is not None, "step4: run 1 fire_claim no longer set")

        require(jobs.advance_next_runs([job_id]) == 1, "step5: advance_next_runs did not advance")
        snap = note(evidence, "step5 stored", job_id)
        s3 = snap["next_run_at"]
        require(datetime.fromisoformat(s3) > datetime.fromisoformat(s2), f"step5: next_run_at {s3} not after S2")
        require(snap["pending_slot"] == s2, f"step5: pending_slot {snap['pending_slot']} != S2 {s2}")

        require(jobs.mark_job_run(job_id, True) is True, "step6: mark_job_run returned False")
        procs.running_owner = None
        after = note(evidence, "step6 stored (after run 1 completes)", job_id)

        reproduced = after["pending_slot"] is None and datetime.fromisoformat(after["next_run_at"]) > datetime.fromisoformat(s2)
        evidence.append(f"S2={s2} S3={s3}; S2 durable record after step 6: {'none' if reproduced else 'present'}")
    return reproduced, evidence


def scenario_fa(root):
    evidence = []
    clock = Clock(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    procs = Processes()
    with jobs.use_cron_store(root):
        install(clock, procs)
        job_id, s1 = new_job(clock, procs)
        s1_dt = datetime.fromisoformat(s1)
        canonical_s1 = cron.occurrences.scheduled_instant(s1)
        evidence.append(f"S1={s1} canonical={canonical_s1}")

        procs.select("A")
        clock.value = s1_dt + timedelta(seconds=10)
        first = due_snapshot(job_id, evidence, "step1 (A)")
        require(first.get("_scheduled_instant") == canonical_s1,
                f"step1: _scheduled_instant {first.get('_scheduled_instant')} != {canonical_s1}")
        require(jobs.advance_next_runs([job_id]) == 1, "step1: advance_next_runs did not advance")
        procs.running_owner = "A"
        snap = note(evidence, "step1 stored (A dispatch queued, no claim)", job_id)
        require(snap["pending_slot"] == s1 and snap["pending_by"] == "A", "step1: stamp missing or not owned by A")
        require(snap["fire_claim_by"] is None, "step1: unexpected fire_claim before worker start")

        clock.advance(constants.FIRE_CLAIM_TTL_SECONDS + 1)
        evidence.append(f"step2: clock advanced {constants.FIRE_CLAIM_TTL_SECONDS + 1}s to {clock.value.isoformat()}")

        procs.select("B")
        second = due_snapshot(job_id, evidence, "step3 (B)")
        evidence.append(f"step3: same _scheduled_instant as step1: {second.get('_scheduled_instant') == first.get('_scheduled_instant')}")
        require(second.get("_scheduled_instant") is not None, "step3: B snapshot has no _scheduled_instant")
        evidence.append(f"step3: B advance_next_runs -> {jobs.advance_next_runs([job_id])}")
        b_claim = jobs.claim_job_for_fire(job_id)
        evidence.append(f"step3: B claim_job_for_fire -> {bool(b_claim)}")
        require(bool(b_claim), "step3: B claim_job_for_fire returned falsy")
        note(evidence, "step3 stored (B claimed)", job_id)
        require(jobs.mark_job_run(job_id, True) is True, "step3: mark_job_run returned False")
        note(evidence, "step3 stored (B completed)", job_id)

        procs.select("A")
        a_claim = jobs.claim_job_for_fire(job_id)
        evidence.append(f"step4: A claim_job_for_fire -> {bool(a_claim)}")
        note(evidence, "step4 stored (A claimed)", job_id)

        reproduced = (
            bool(b_claim) and bool(a_claim)
            and first.get("_scheduled_instant") == second.get("_scheduled_instant")
        )
    return reproduced, evidence


def main():
    scenarios = (("F-A", scenario_fa), ("F-B", scenario_fb))
    results = {}
    harness_failed = False
    try:
        for name, runner in scenarios:
            root = Path(SCRATCH_HOME) / name.lower()
            root.mkdir(parents=True, exist_ok=True)
            try:
                results[name] = runner(root)
            except HarnessError as exc:
                harness_failed = True
                print(f"{name} HARNESS ERROR: {exc}", file=sys.stderr)
        for name, _ in scenarios:
            if name not in results:
                continue
            reproduced, evidence = results[name]
            print(f"{name} {'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
            for line in evidence:
                print(f"  {line}")
    finally:
        shutil.rmtree(SCRATCH_HOME, ignore_errors=True)
    if harness_failed:
        return EXIT_HARNESS_ERROR
    return EXIT_REPRODUCED if all(r for r, _ in results.values()) else EXIT_NOT_REPRODUCED


if __name__ == "__main__":
    sys.exit(main())
