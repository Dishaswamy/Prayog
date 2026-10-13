"""PRAYOG-49 — eligibility evaluator (FR-GPU-030 to 035, 118, 183)."""

from scheduler.domain import NodeState, QueueReason
from scheduler.eligibility import evaluate, ineligibility_reason, is_eligible, usable_units
from tests.scheduler.factories import CENTRAL, COURSE_GROUP, NLP, NOW, ROBOTICS, job, node, window

# ── teaching-pool exact match (FR-GPU-032) ─────────────────────────────

def test_research_job_never_reaches_teaching_pool():                # A-61
    teaching = node(owner=CENTRAL, teaching=True)
    result = evaluate(job(lab=NLP), teaching, NOW)
    assert not result.eligible
    assert QueueReason.NO_ELIGIBLE_NODE in result.reasons


def test_course_job_runs_only_on_teaching_pool():
    assert is_eligible(job(lab=COURSE_GROUP, course=True), node(owner=CENTRAL, teaching=True), NOW)
    assert not is_eligible(job(lab=COURSE_GROUP, course=True), node(owner=NLP), NOW)


# ── donation mode (FR-GPU-033) ─────────────────────────────────────────

def test_reserved_node_admits_only_its_owner():
    reserved = node(owner=ROBOTICS, shared=False)
    assert is_eligible(job(lab=ROBOTICS), reserved, NOW)
    assert not is_eligible(job(lab=NLP), reserved, NOW)


def test_shared_node_admits_guests():
    assert is_eligible(job(lab=NLP), node(owner=ROBOTICS, shared=True), NOW)


# ── node state (FR-GPU-118) ────────────────────────────────────────────

def test_only_available_nodes_are_eligible():
    for state in NodeState:
        expected = state == NodeState.AVAILABLE
        assert is_eligible(job(), node(state=state), NOW) is expected, state


# ── capacity and single node (FR-GPU-031, 183) ─────────────────────────

def test_too_few_free_units_is_insufficient_not_ineligible_forever():
    result = evaluate(job(gpus=3), node(units=4, free=2), NOW)
    assert not result.eligible
    assert result.reasons == (QueueReason.INSUFFICIENT_FREE_UNITS,)


def test_job_never_spans_nodes():                                      # A-80
    two_small = [node(node_id=1, units=2), node(node_id=2, units=2)]
    big_job = job(gpus=4)
    assert not any(is_eligible(big_job, n, NOW) for n in two_small)
    assert ineligibility_reason(big_job, two_small, NOW) == QueueReason.NO_ELIGIBLE_NODE


def test_exactly_enough_units_is_eligible():
    assert is_eligible(job(gpus=4), node(units=4, free=4), NOW)


# ── maintenance (FR-GPU-034) ───────────────────────────────────────────

def test_window_starting_before_walltime_ends_blocks_units():
    blocked = node(units=2, maintenance=(window(start_in_h=3),))       # job runs 4 h
    result = evaluate(job(gpus=2, walltime_h=4), blocked, NOW)
    assert result.reasons == (QueueReason.MAINTENANCE_WINDOW,)


def test_window_after_walltime_does_not_block():
    later = node(units=2, maintenance=(window(start_in_h=5),))
    assert is_eligible(job(gpus=2, walltime_h=4), later, NOW)


def test_finished_window_does_not_block():
    past = node(units=2, maintenance=(window(start_in_h=-3, length_h=1),))
    assert is_eligible(job(gpus=2, walltime_h=4), past, NOW)


def test_window_in_progress_blocks():
    now_window = node(units=2, maintenance=(window(start_in_h=-1, length_h=2),))
    assert not is_eligible(job(gpus=2, walltime_h=1), now_window, NOW)


def test_usable_units_counts_only_free_unblocked_units():
    assert len(usable_units(job(), node(units=4, free=3), NOW)) == 3


# ── queue reasons (FR-GPU-021, 144) ────────────────────────────────────

def test_reason_reflects_nodes_the_job_could_actually_use():
    nodes = [
        node(node_id=1, owner=NLP, units=4, free=1),                   # busy
        node(node_id=2, owner=NLP, units=4, free=0),                   # busy
        node(node_id=3, owner=CENTRAL, teaching=True),                 # never reachable for research
    ]
    assert ineligibility_reason(job(gpus=2), nodes, NOW) == QueueReason.INSUFFICIENT_FREE_UNITS


def test_all_reasons_are_reported_not_just_the_first():
    down_and_full = node(owner=ROBOTICS, shared=False, units=4, free=0, state=NodeState.DOWN)
    reasons = evaluate(job(lab=NLP), down_and_full, NOW).reasons
    assert QueueReason.NO_ELIGIBLE_NODE in reasons
    assert QueueReason.INSUFFICIENT_FREE_UNITS in reasons
