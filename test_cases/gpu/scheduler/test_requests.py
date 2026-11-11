"""Submission rules (GPU requirements section 5). Field rules are pure; the rest needs the database."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select

import models as orm
from logic.common.errors import ConflictError, NotFoundError
from logic.gpu.scheduler.claim import ClaimStatus, claim
from logic.gpu.scheduler.config import SchedulerConfig
from logic.gpu.scheduler.domain import AccountType, JobState, PriorityClass
from logic.gpu.scheduler.requests import (
    RejectCode,
    SubmissionRejected,
    edit_job,
    format_walltime,
    parse_gpus,
    parse_walltime,
    quota_status,
    submit_job,
)
from logic.gpu.scheduler.scoring import score_job
from test_cases.gpu.scheduler.db_factories import broken_invariants
from test_cases.gpu.scheduler.factories import NOW
from test_cases.gpu.scheduler.factories import job as job_view

# ── field rules (no database) ──────────────────────────────────────────

@pytest.mark.parametrize("value", [1, 8, 64])
def test_positive_whole_gpu_counts_are_accepted(value):  # FR-GPU-010
    assert parse_gpus(value) == value


@pytest.mark.parametrize("value", [0, -2, 2.5, "2", True, None, [2]])
def test_anything_else_is_invalid_gpus(value):  # FR-GPU-010, FR-GPU-016
    with pytest.raises(SubmissionRejected) as rejected:
        parse_gpus(value)
    assert rejected.value.code == RejectCode.INVALID_GPUS and rejected.value.status_code == 400


@pytest.mark.parametrize("value, minutes", [("04:00", 240), ("1:30", 90), ("00:01", 1), ("48:00", 2880), ("60:00", 3600)])
def test_walltime_is_hours_and_minutes(value, minutes):  # FR-GPU-011 (the 48:00 ceiling is checked separately)
    assert parse_walltime(value) == minutes


@pytest.mark.parametrize("value", ["00:00", "4", "4h", "04:60", "04:5", "-1:00", "", 240, None])
def test_malformed_or_zero_walltime_is_invalid(value):  # FR-GPU-011, FR-GPU-016
    with pytest.raises(SubmissionRejected, match="at least 00:01") as rejected:
        parse_walltime(value)
    assert rejected.value.code == RejectCode.INVALID_WALLTIME


def test_minimum_walltime_is_configuration():  # NFR-GPU-053
    with pytest.raises(SubmissionRejected, match="at least 00:10"):
        parse_walltime("00:05", SchedulerConfig(min_walltime_minutes=10))


@pytest.mark.parametrize("minutes, text", [(0, "00:00"), (90, "01:30"), (2880, "48:00"), (6000, "100:00")])
def test_format_walltime(minutes, text):
    assert format_walltime(minutes) == text


# ── submit (database) ──────────────────────────────────────────────────

def submit(db, user, gpus=2, walltime="04:00", request="req-1", now=NOW, **kwargs):
    return submit_job(db, user, gpus, walltime, request, now=now, **kwargs)


def job_count(db):
    return db.scalar(select(func.count()).select_from(orm.Job))


@pytest.mark.db
def test_accepted_job_is_queued_with_server_time_and_the_server_side_context(db, seed, labs):  # FR-GPU-012, 017
    seed.node(labs["NLP"], units=4)
    user = seed.member(labs["NLP"])
    seed.commit()

    result = submit(db, user, project_ref="proj-7", snapshot_hash="ab" * 32)

    job = result.job
    assert result.created and job.state == JobState.QUEUED
    assert job.submitted_at == job.effective_submit_at == NOW
    assert (job.lab_id, job.account_type) == (labs["NLP"], AccountType.RESEARCH)
    assert (job.gpus, job.walltime_minutes, job.project_ref) == (2, 240, "proj-7")


@pytest.mark.db
def test_a_retry_within_the_window_returns_the_original(db, seed, labs):  # A-24, FR-GPU-107
    seed.node(labs["NLP"], units=4)
    user = seed.member(labs["NLP"])
    seed.commit()

    first = submit(db, user)
    retry = submit(db, user, gpus=3, now=NOW + timedelta(hours=23))           # even with a different body

    assert not retry.created and retry.job.id == first.job.id
    assert job_count(db) == 1


@pytest.mark.db
def test_reusing_a_request_id_after_the_window_is_refused(db, seed, labs):  # FR-GPU-107
    seed.node(labs["NLP"], units=4)
    user = seed.member(labs["NLP"])
    seed.commit()
    submit(db, user, now=NOW - timedelta(hours=25))

    with pytest.raises(ConflictError):
        submit(db, user)
    db.rollback()
    assert job_count(db) == 1


@pytest.mark.db
def test_the_same_request_id_from_two_users_is_two_jobs(db, seed, labs):  # FR-GPU-107 (per user)
    seed.node(labs["NLP"], units=4)
    alice, bob = seed.member(labs["NLP"]), seed.member(labs["NLP"])
    seed.commit()

    assert submit(db, alice).job.id != submit(db, bob).job.id


@pytest.mark.db
def test_queue_limit_counts_only_active_jobs(db, seed, labs):  # FR-GPU-105
    seed.node(labs["NLP"], units=4)
    user = seed.member(labs["NLP"])
    for state in (JobState.QUEUED, JobState.RUNNING, JobState.COMPLETED, JobState.CANCELLED):
        seed.job(labs["NLP"], state=state, user=user)
    seed.commit()
    cfg = SchedulerConfig(max_active_jobs_per_user=3)

    submit(db, user, request="third", cfg=cfg)                                  # 2 active + 1 = 3: allowed
    with pytest.raises(SubmissionRejected, match="already have 3 active jobs; the limit is 3") as rejected:
        submit(db, user, request="fourth", cfg=cfg)
    assert rejected.value.code == RejectCode.QUEUE_LIMIT_REACHED


@pytest.mark.db
def test_concurrent_submissions_cannot_beat_the_queue_limit(db, db_sessions, seed, labs):  # FR-GPU-105
    seed.node(labs["NLP"], units=4)
    user = seed.member(labs["NLP"])
    seed.commit()
    cfg = SchedulerConfig(max_active_jobs_per_user=1)
    barrier = threading.Barrier(4)
    outcomes = []

    def attempt(i):
        with db_sessions() as session:
            barrier.wait(timeout=10)
            try:
                outcomes.append(submit(session, user, request=f"req-{i}", cfg=cfg).created)
            except SubmissionRejected as exc:
                outcomes.append(exc.code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(map(str, outcomes)) == ["QUEUE_LIMIT_REACHED"] * 3 + ["True"]
    assert job_count(db) == 1


# ── edit (FR-GPU-070 to 072, FR-GPU-130 to 132) ────────────────────────

def queued_job(db, seed, labs, gpus=2, walltime="04:00", waited_h=2):
    seed.node(labs["NLP"], units=8)
    user = seed.member(labs["NLP"])
    seed.commit()
    j = submit(db, user, gpus=gpus, walltime=walltime, request=f"q-{user}", now=NOW - timedelta(hours=waited_h)).job
    return user, j.id


def reload(db, job_id):
    db.expire_all()
    return db.get(orm.Job, job_id)


@pytest.mark.db
@pytest.mark.parametrize("change", [{"gpus": 1}, {"walltime": "02:00"}, {"gpus": 2, "walltime": "04:00"}, {}])
def test_an_edit_that_does_not_grow_keeps_the_submit_time(db, seed, labs, change):  # A-15, FR-GPU-071
    user, j = queued_job(db, seed, labs)
    before = reload(db, j).effective_submit_at

    edit_job(db, user, j, now=NOW, **change)

    assert reload(db, j).effective_submit_at == before


@pytest.mark.db
@pytest.mark.parametrize("change", [{"gpus": 3}, {"walltime": "04:01"}, {"gpus": 1, "walltime": "08:00"}])
def test_an_edit_that_grows_resets_the_submit_time(db, seed, labs, change):  # A-16, FR-GPU-072
    user, j = queued_job(db, seed, labs)

    edit_job(db, user, j, now=NOW, **change)

    row = reload(db, j)
    assert row.effective_submit_at == NOW
    assert row.submitted_at == NOW - timedelta(hours=2)                       # the audit time never changes


@pytest.mark.db
def test_an_edit_changes_the_request_and_the_snapshot(db, seed, labs):  # FR-GPU-070, FR-GPU-132
    user, j = queued_job(db, seed, labs)

    edit_job(db, user, j, gpus=1, walltime="01:30", snapshot_hash="cd" * 32, now=NOW)

    row = reload(db, j)
    assert (row.gpus, row.walltime_minutes, row.snapshot_hash) == (1, 90, "cd" * 32)


@pytest.mark.db
@pytest.mark.parametrize("change, code", [
    ({"gpus": 0}, RejectCode.INVALID_GPUS),
    ({"walltime": "4h"}, RejectCode.INVALID_WALLTIME),
    ({"walltime": "49:00"}, RejectCode.WALLTIME_EXCEEDS_MAX),
    ({"gpus": 9}, RejectCode.GPUS_EXCEED_MAX_NODE),
])
def test_a_rejected_edit_leaves_the_job_unchanged(db, seed, labs, change, code):  # FR-GPU-130
    user, j = queued_job(db, seed, labs)
    row = reload(db, j)
    before = (row.gpus, row.walltime_minutes, row.effective_submit_at)

    with pytest.raises(SubmissionRejected) as rejected:
        edit_job(db, user, j, now=NOW, **change)

    assert rejected.value.code == code
    row = reload(db, j)
    assert (row.gpus, row.walltime_minutes, row.effective_submit_at) == before


@pytest.mark.db
@pytest.mark.parametrize("state", [JobState.SCHEDULED, JobState.RUNNING, JobState.CANCELLED])
def test_only_queued_jobs_can_be_edited(db, seed, labs, state):  # FR-GPU-070, FR-GPU-131
    user, j = queued_job(db, seed, labs)
    reload(db, j).state = state
    db.commit()

    with pytest.raises(SubmissionRejected, match="only QUEUED jobs") as rejected:
        edit_job(db, user, j, gpus=1, now=NOW)
    assert (rejected.value.code, rejected.value.status_code) == (RejectCode.JOB_NOT_QUEUED, 409)


@pytest.mark.db
def test_someone_elses_job_cannot_be_edited(db, seed, labs):  # NFR-GPU-030
    _, j = queued_job(db, seed, labs)
    other = seed.member(labs["NLP"])
    seed.commit()
    with pytest.raises(NotFoundError):
        edit_job(db, other, j, gpus=1, now=NOW)


@pytest.mark.db
def test_an_edit_and_a_claim_racing_on_one_job_never_both_succeed(db, db_sessions, seed, labs):  # A-67, FR-GPU-131
    for _ in range(15):
        user, j = queued_job(db, seed, labs, gpus=2)
        node = db.scalar(select(orm.Node.id).order_by(orm.Node.id.desc()).limit(1))
        barrier = threading.Barrier(2)
        outcomes = {}

        def do_edit(user=user, j=j, barrier=barrier, outcomes=outcomes):
            with db_sessions() as s:
                barrier.wait(timeout=10)
                try:
                    edit_job(s, user, j, gpus=1, now=NOW)
                    outcomes["edit"] = "ok"
                except SubmissionRejected as exc:
                    outcomes["edit"] = exc.code

        def do_claim(j=j, node=node, barrier=barrier, outcomes=outcomes):
            with db_sessions() as s:
                barrier.wait(timeout=10)
                outcomes["claim"] = claim(s, j, node, PriorityClass.P0, score_job(job_view(), NOW),
                                          pass_id="race", trigger="test", now=NOW).status

        threads = [threading.Thread(target=do_edit), threading.Thread(target=do_claim)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        row = reload(db, j)
        held = db.scalar(select(func.count()).select_from(orm.AllocationUnit).join(orm.Allocation).where(
            orm.Allocation.job_id == j, orm.Allocation.released_at.is_(None)))
        if outcomes["edit"] == "ok":
            assert row.gpus == 1                      # the edit applied, and before any placement
        else:
            assert outcomes["edit"] == RejectCode.JOB_NOT_QUEUED and row.gpus == 2
        if outcomes["claim"] == ClaimStatus.CLAIMED:
            assert row.state == JobState.SCHEDULED and held == row.gpus   # placed exactly as it stands
        else:
            assert outcomes["claim"] == ClaimStatus.JOB_UNAVAILABLE and row.state == JobState.QUEUED and held == 0
        assert broken_invariants(db) == {}


# ── course cap (FR-GPU-104, FR-GPU-178) ────────────────────────────────

def course_member(seed, labs, cap=10.0):
    seed.node(labs["CENTRAL"], units=4, teaching=True)
    user = seed.member(labs["COURSE"], course=True, cap=cap)
    seed.commit()
    return user


@pytest.mark.db
def test_a_request_within_the_remaining_cap_is_accepted(db, seed, labs):  # FR-GPU-104
    user = course_member(seed, labs, cap=10)
    seed.usage(labs["COURSE"], labs["CENTRAL"], gpu_hours=4, user=user)
    seed.commit()

    assert submit(db, user, gpus=2, walltime="03:00").created                # exactly the 6 left


@pytest.mark.db
def test_over_the_remaining_cap_is_quota_exceeded(db, seed, labs):  # A-25, FR-GPU-104
    user = course_member(seed, labs, cap=10)
    seed.usage(labs["COURSE"], labs["CENTRAL"], gpu_hours=4, user=user)       # 4 consumed
    seed.commit()
    submit(db, user, gpus=1, walltime="02:00", request="a")                  # 2 committed

    with pytest.raises(SubmissionRejected, match="Requested 6 GPU-hours, but your sponsorship has 4 of its 10") as r:
        submit(db, user, gpus=2, walltime="03:00", request="b")
    assert r.value.code == RejectCode.QUOTA_EXCEEDED


@pytest.mark.db
def test_the_cap_is_shared_by_the_whole_sponsorship(db, seed, labs):  # FR-GPU-104
    alice = course_member(seed, labs, cap=5)
    bob = seed.member(labs["COURSE"], course=True, cap=5)
    seed.commit()
    submit(db, alice, gpus=1, walltime="04:00")

    with pytest.raises(SubmissionRejected, match="has 1 of its 5"):
        submit(db, bob, gpus=1, walltime="02:00")


@pytest.mark.db
def test_uncharged_runs_and_finished_jobs_do_not_use_the_cap(db, seed, labs):  # FR-GPU-104, 143
    user = course_member(seed, labs, cap=4)
    seed.usage(labs["COURSE"], labs["CENTRAL"], gpu_hours=3, user=user, charged=False)   # interrupted run
    seed.job(labs["COURSE"], gpus=4, walltime_h=4, course=True, state=JobState.COMPLETED, user=user)
    seed.commit()

    assert submit(db, user, gpus=1, walltime="04:00").created


@pytest.mark.db
def test_an_edit_is_checked_against_the_cap_without_counting_itself(db, seed, labs):  # FR-GPU-130, 104
    user = course_member(seed, labs, cap=8)
    j = submit(db, user, gpus=2, walltime="04:00").job.id                    # all 8 committed

    edit_job(db, user, j, walltime="03:00", now=NOW)                          # 6: fine, its own 8 is not counted
    with pytest.raises(SubmissionRejected) as r:
        edit_job(db, user, j, gpus=3, now=NOW)                                # 9 > 8
    assert r.value.code == RejectCode.QUOTA_EXCEEDED


@pytest.mark.db
def test_research_accounts_have_no_cap(db, seed, labs):
    seed.node(labs["NLP"], units=4)
    user = seed.member(labs["NLP"], cap=1)                                    # ignored for research
    seed.commit()
    assert submit(db, user, gpus=4, walltime="48:00").created


@pytest.mark.db
def test_quota_status_shows_cap_use_and_members(db, seed, labs):  # FR-GPU-178
    alice = course_member(seed, labs, cap=20)
    bob = seed.member(labs["COURSE"], course=True, cap=20)
    seed.usage(labs["COURSE"], labs["CENTRAL"], gpu_hours=3, user=alice)
    seed.usage(labs["COURSE"], labs["CENTRAL"], gpu_hours=1.5, user=bob)
    seed.commit()
    submit(db, alice, gpus=1, walltime="02:00")

    q = quota_status(db, alice)

    assert (q.cap, q.consumed, q.committed, q.remaining) == (20, 4.5, 2, 13.5)
    assert sorted(hours for _, hours in q.members) == [1.5, 3.0]


@pytest.mark.db
def test_quota_status_is_only_for_capped_course_accounts(db, seed, labs):  # FR-GPU-178
    seed.node(labs["NLP"], units=4)
    researcher = seed.member(labs["NLP"])
    seed.commit()
    with pytest.raises(NotFoundError):
        quota_status(db, researcher)
