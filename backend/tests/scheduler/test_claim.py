"""
The claim transaction against a real PostgreSQL (GPU requirements section 7.4).

Concurrency is real: each racing pass is a thread with its own session and
connection, released together by a barrier.
"""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DataError, IntegrityError

from scheduler import models as orm
from scheduler.claim import ClaimStatus, claim, utc_now
from scheduler.domain import JobState, NodeState, PriorityClass, QueueReason
from scheduler.scoring import combine_terms, score_job
from tests.scheduler.db_factories import Seed, broken_invariants
from tests.scheduler.factories import NOW, job

pytestmark = pytest.mark.db

P0, P1, P2, P3 = PriorityClass

# A realistic research breakdown: 90 min wait, 2 GPUs for 4 h.
BREAKDOWN = score_job(job(gpus=2, walltime_h=4, waited_min=90), NOW)


@pytest.fixture
def seed(db):
    return Seed(db)


@pytest.fixture
def labs(seed):
    return {name: seed.lab(name) for name in ("NLP", "ROBOTICS", "VISION")} | {
        "CENTRAL": seed.lab("CENTRAL", central=True),
        "COURSE": seed.lab("CS455 course group"),
    }


def run_claim(db_sessions, job_id, node_id, cls=P0, pass_id="pass-1"):
    with db_sessions() as session:
        return claim(session, job_id, node_id, cls, BREAKDOWN, pass_id=pass_id, trigger="test", now=NOW)


def job_state(db, job_id):
    db.expire_all()
    return db.get(orm.Job, job_id).state


def live_allocations(db):
    return db.scalars(select(orm.Allocation).where(orm.Allocation.released_at.is_(None))).all()


def race(db_sessions, attempts):
    """Run (job_id, node_id, cls) claims at the same instant, one thread each."""
    barrier = threading.Barrier(len(attempts))
    outcomes = [None] * len(attempts)
    errors = []

    def attempt(i, job_id, node_id, cls):
        try:
            with db_sessions() as session:
                barrier.wait(timeout=10)
                outcomes[i] = claim(session, job_id, node_id, cls, BREAKDOWN, pass_id=f"pass-{i}", trigger="test",
                                    now=NOW)
        except Exception as exc:  # noqa: BLE001  surfaced below; a thread's exception is otherwise lost
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(i, *a)) for i, a in enumerate(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return outcomes


# ── a successful claim ─────────────────────────────────────────────────

def test_claim_takes_the_lowest_free_units_and_schedules_the_job(db, db_sessions, seed, labs):  # FR-GPU-054, 057
    node = seed.node(labs["NLP"], units=4)
    j = seed.job(labs["NLP"], gpus=2)
    units = seed.unit_ids(node)
    seed.commit()

    outcome = run_claim(db_sessions, j, node)

    assert outcome.status == ClaimStatus.CLAIMED and outcome.queue_reason is None
    assert outcome.unit_ids == tuple(units[:2])
    assert job_state(db, j) == JobState.SCHEDULED
    [allocation] = live_allocations(db)
    assert (allocation.job_id, allocation.node_id, allocation.claimed_at) == (j, node, NOW)
    assert allocation.acknowledged_at is None                       # dispatch not acknowledged yet
    markers = db.scalars(select(orm.GpuUnit.current_allocation_id).order_by(orm.GpuUnit.unit_index)).all()
    assert markers == [allocation.id, allocation.id, None, None]
    assert broken_invariants(db) == {}


def test_decision_records_every_input_and_reproduces_the_score(db, db_sessions, seed, labs):  # FR-GPU-098, A-57
    node = seed.node(labs["NLP"])
    j = seed.job(labs["NLP"])
    seed.commit()

    outcome = run_claim(db_sessions, j, node, cls=P0, pass_id="pass-42")

    d = db.scalars(select(orm.SchedulingDecision)).one()
    assert (d.pass_id, d.trigger, d.job_id, d.node_id, d.allocation_id) == ("pass-42", "test", j, node,
                                                                              outcome.allocation_id)
    assert d.priority_class == P0 and d.escalated is False
    assert (d.age, d.fair_share, d.contribution, d.size) == (
        BREAKDOWN.age, BREAKDOWN.fair_share, BREAKDOWN.contribution, BREAKDOWN.size
    )
    assert d.score == BREAKDOWN.score
    assert combine_terms(d.age, d.size, d.fair_share, d.contribution) == d.score


# ── all or nothing ─────────────────────────────────────────────────────

def test_four_requested_with_only_three_lockable_holds_nothing(db, db_sessions, seed, labs):  # A-05, FR-GPU-054
    node = seed.node(labs["NLP"], units=4)
    j = seed.job(labs["NLP"], gpus=4)
    units = seed.unit_ids(node)
    seed.commit()

    with db_sessions() as other_pass:            # another transaction holds unit 3's row lock
        other_pass.execute(text("SELECT id FROM gpu_units WHERE id = :u FOR UPDATE"), {"u": units[3]})

        outcome = run_claim(db_sessions, j, node)

        assert outcome.status == ClaimStatus.LOST_CLAIM_RACE
        assert outcome.queue_reason == QueueReason.LOST_CLAIM_RACE
        assert job_state(db, j) == JobState.QUEUED                    # FR-GPU-056
        assert live_allocations(db) == []
        # Nothing held: the three units the claim saw are lockable again at once.
        locked = other_pass.execute(
            text("SELECT id FROM gpu_units WHERE id = ANY(:u) FOR UPDATE NOWAIT"), {"u": units[:3]}
        ).scalars().all()
        assert sorted(locked) == sorted(units[:3])
        other_pass.rollback()

    assert run_claim(db_sessions, j, node).claimed                    # all four once the lock is gone


def test_a_write_failing_mid_claim_rolls_everything_back(db, db_sessions, seed, labs):  # FR-GPU-052/054
    node = seed.node(labs["NLP"], units=4)
    j = seed.job(labs["NLP"], gpus=2)
    seed.commit()

    with pytest.raises(DataError), db_sessions() as session:   # pass_id longer than its column: the last write fails
        claim(session, j, node, P0, BREAKDOWN, pass_id="x" * 37, trigger="test", now=NOW)

    assert job_state(db, j) == JobState.QUEUED
    assert live_allocations(db) == []
    assert broken_invariants(db) == {}
    assert run_claim(db_sessions, j, node).claimed                    # no lock or row left behind


def test_claim_defaults_to_the_current_utc_time(db, db_sessions, seed, labs):  # FR-GPU-057
    node = seed.node(labs["NLP"])
    j = seed.job(labs["NLP"])
    seed.commit()

    before = utc_now()
    with db_sessions() as session:
        assert claim(session, j, node, P0, BREAKDOWN, pass_id="pass-1", trigger="test").claimed
    [allocation] = live_allocations(db)
    assert allocation.claimed_at.tzinfo is None                       # naive UTC, like every timestamp
    assert before <= allocation.claimed_at <= utc_now()


def test_a_unit_already_allocated_is_not_free(db, db_sessions, seed, labs):  # A-05, FR-GPU-054
    node = seed.node(labs["NLP"], units=4)
    first = seed.job(labs["NLP"], gpus=1)
    second = seed.job(labs["NLP"], gpus=4)
    seed.commit()

    assert run_claim(db_sessions, first, node).claimed
    assert run_claim(db_sessions, second, node).status == ClaimStatus.LOST_CLAIM_RACE
    assert job_state(db, second) == JobState.QUEUED
    assert broken_invariants(db) == {}


@pytest.mark.parametrize("starts_in_h, claimed", [
    (1, False),       # opens before the 4 h job would end: unit unusable
    (-1, False),      # already open (a freeze)
    (4, True),        # opens exactly when the job would end
    (-3, True),       # already over (a 2 h window that started 3 h ago)
])
def test_maintenance_overlapping_the_walltime_blocks_a_unit(db, db_sessions, seed, labs, starts_in_h, claimed):
    # FR-GPU-034
    node = seed.node(labs["NLP"], units=4)
    j = seed.job(labs["NLP"], gpus=4, walltime_h=4)
    seed.window(seed.unit_ids(node)[2], start_in_h=starts_in_h)
    seed.commit()

    assert run_claim(db_sessions, j, node).claimed is claimed


# ── the job and the node are re-checked under lock ─────────────────────

@pytest.mark.parametrize("state", [NodeState.DRAINING, NodeState.DOWN, NodeState.PENDING_AGENT])
def test_node_that_left_available_is_not_claimed(db, db_sessions, seed, labs, state):  # FR-GPU-118
    node = seed.node(labs["NLP"], state=state)
    j = seed.job(labs["NLP"])
    seed.commit()

    outcome = run_claim(db_sessions, j, node)

    assert outcome.status == ClaimStatus.NODE_UNAVAILABLE
    assert outcome.queue_reason == QueueReason.NO_ELIGIBLE_NODE
    assert job_state(db, j) == JobState.QUEUED


@pytest.mark.parametrize("state", [JobState.CANCELLED, JobState.COMPLETED])
def test_job_no_longer_queued_is_not_claimed(db, db_sessions, seed, labs, state):
    node = seed.node(labs["NLP"])
    j = seed.job(labs["NLP"], state=state)
    seed.commit()

    outcome = run_claim(db_sessions, j, node)

    assert outcome.status == ClaimStatus.JOB_UNAVAILABLE and outcome.queue_reason is None
    assert live_allocations(db) == []


def test_job_held_by_another_pass_is_skipped_without_waiting(db, db_sessions, seed, labs):  # FR-GPU-053
    node = seed.node(labs["NLP"])
    j = seed.job(labs["NLP"])
    seed.commit()

    with db_sessions() as other_pass:
        other_pass.execute(text("SELECT id FROM jobs WHERE id = :j FOR UPDATE"), {"j": j})
        assert run_claim(db_sessions, j, node).status == ClaimStatus.JOB_UNAVAILABLE   # would hang without SKIP LOCKED
        other_pass.rollback()


# ── concurrency ────────────────────────────────────────────────────────

ROUNDS = 25
PASSES = 8


def test_passes_racing_for_the_last_free_unit_allocate_it_once(db, db_sessions, seed, labs):  # A-06, FR-GPU-055/056
    for _ in range(ROUNDS):
        node = seed.node(labs["NLP"], units=4)
        filler = seed.job(labs["NLP"], gpus=3)
        racers = [seed.job(labs["NLP"], gpus=1) for _ in range(PASSES)]
        seed.commit()
        assert run_claim(db_sessions, filler, node).claimed           # leaves exactly one free unit

        outcomes = race(db_sessions, [(j, node, P0) for j in racers])

        statuses = sorted(o.status for o in outcomes)
        assert statuses == [ClaimStatus.CLAIMED] + [ClaimStatus.LOST_CLAIM_RACE] * (PASSES - 1)
        db.expire_all()
        assert broken_invariants(db) == {}
        assert sum(job_state(db, j) == JobState.SCHEDULED for j in racers) == 1


def test_passes_claiming_one_job_on_different_nodes_allocate_it_once(db, db_sessions, seed, labs):  # I2
    for _ in range(ROUNDS):
        nodes = [seed.node(labs["NLP"], units=2) for _ in range(2)]
        j = seed.job(labs["NLP"], gpus=1)
        seed.commit()

        outcomes = race(db_sessions, [(j, nodes[i % 2], P0) for i in range(PASSES)])

        statuses = sorted(o.status for o in outcomes)
        assert statuses == [ClaimStatus.CLAIMED] + [ClaimStatus.JOB_UNAVAILABLE] * (PASSES - 1)
        db.expire_all()
        assert broken_invariants(db) == {}


# ── the database backstop ──────────────────────────────────────────────

def test_partial_index_rejects_a_second_live_allocation_on_a_unit(db, seed, labs):  # FR-GPU-055, I1
    node = seed.node(labs["NLP"], units=1)
    [unit] = seed.unit_ids(node)
    a, b = (orm.Allocation(job_id=seed.job(labs["NLP"], gpus=1), node_id=node, claimed_at=NOW) for _ in range(2))
    db.add_all([a, b])
    db.flush()
    db.add(orm.AllocationUnit(allocation_id=a.id, unit_id=unit))
    db.flush()

    db.add(orm.AllocationUnit(allocation_id=b.id, unit_id=unit))
    with pytest.raises(IntegrityError, match="ux_alloc_units_live"):
        db.flush()
    db.rollback()


def test_partial_index_allows_reuse_after_release(db, seed, labs):  # FR-GPU-055
    node = seed.node(labs["NLP"], units=1)
    [unit] = seed.unit_ids(node)
    released = NOW + timedelta(hours=1)
    old = orm.Allocation(job_id=seed.job(labs["NLP"], gpus=1), node_id=node, claimed_at=NOW, released_at=released)
    new = orm.Allocation(job_id=seed.job(labs["NLP"], gpus=1), node_id=node, claimed_at=NOW)
    db.add_all([old, new])
    db.flush()
    db.add_all([
        orm.AllocationUnit(allocation_id=old.id, unit_id=unit, released_at=released),
        orm.AllocationUnit(allocation_id=new.id, unit_id=unit),
    ])
    db.flush()                                                     # no error


def test_partial_index_rejects_a_second_live_allocation_for_a_job(db, seed, labs):  # I2
    node = seed.node(labs["NLP"])
    j = seed.job(labs["NLP"])
    db.add(orm.Allocation(job_id=j, node_id=node, claimed_at=NOW))
    db.flush()

    db.add(orm.Allocation(job_id=j, node_id=node, claimed_at=NOW))
    with pytest.raises(IntegrityError, match="ux_allocations_live"):
        db.flush()
    db.rollback()


# ── owner-claim guard ──────────────────────────────────────────────────

@pytest.mark.parametrize("cls", [P1, P2])
def test_guest_claim_rolls_back_when_the_owner_has_an_eligible_job(db, db_sessions, seed, labs, cls):  # A-58
    rob_02 = seed.node(labs["ROBOTICS"], units=4, shared=True)
    guest = seed.job(labs["NLP"], gpus=2, waited_min=60)
    kavya = seed.job(labs["ROBOTICS"], gpus=4)          # submitted after the guest's pass read the queue
    seed.commit()

    outcome = run_claim(db_sessions, guest, rob_02, cls=cls)

    assert outcome.status == ClaimStatus.OWNER_CLAIM_PENDING          # FR-GPU-186
    assert outcome.queue_reason == QueueReason.OWNER_CLAIM_PENDING
    assert job_state(db, guest) == JobState.QUEUED
    assert live_allocations(db) == []                                 # no unit held
    assert run_claim(db_sessions, kavya, rob_02, cls=P0).claimed      # the owner's job takes the node next pass


def test_guard_ignores_owner_work_that_cannot_use_the_node(db, db_sessions, seed, labs):  # A-59
    rob_02 = seed.node(labs["ROBOTICS"], units=4, shared=True)
    seed.window(seed.unit_ids(rob_02)[3], start_in_h=1)                # unit 3 unusable for 4 h jobs
    seed.job(labs["ROBOTICS"], gpus=4, walltime_h=4)                  # needs all 4: not eligible now
    seed.job(labs["ROBOTICS"], gpus=8)                                # larger than the node: never eligible
    guest = seed.job(labs["NLP"], gpus=2, walltime_h=0.5)
    seed.commit()

    assert run_claim(db_sessions, guest, rob_02, cls=P1).claimed


def test_guard_ignores_owner_work_already_scheduled(db, db_sessions, seed, labs):  # FR-GPU-186 (QUEUED only)
    rob_02 = seed.node(labs["ROBOTICS"], units=4, shared=True)
    seed.job(labs["ROBOTICS"], gpus=1, state=JobState.RUNNING)
    guest = seed.job(labs["NLP"], gpus=2)
    seed.commit()

    assert run_claim(db_sessions, guest, rob_02, cls=P2).claimed


def test_guard_is_a_no_op_for_the_owners_own_job(db, db_sessions, seed, labs):  # FR-GPU-186, P0
    rob_02 = seed.node(labs["ROBOTICS"], units=4)
    first = seed.job(labs["ROBOTICS"], gpus=2)
    seed.job(labs["ROBOTICS"], gpus=2)                                # another owner job, also eligible
    seed.commit()

    assert run_claim(db_sessions, first, rob_02, cls=P0).claimed


def test_guard_is_a_no_op_for_the_teaching_pool(db, db_sessions, seed, labs):  # FR-GPU-186, P3
    teach_01 = seed.node(labs["CENTRAL"], units=4, shared=True, teaching=True)
    seed.job(labs["CENTRAL"], gpus=1, course=True)                    # would trip the guard if it applied
    student = seed.job(labs["COURSE"], gpus=1, course=True)
    seed.commit()

    assert run_claim(db_sessions, student, teach_01, cls=P3).claimed
