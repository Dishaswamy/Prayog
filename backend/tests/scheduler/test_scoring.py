"""PRAYOG-51 — fair-share scoring with deterministic ordering (FR-GPU-043 to 050, 119, 182, 185)."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scheduler.config import DEFAULT_CONFIG, ConfigError, SchedulerConfig
from scheduler.scoring import (
    LabUsage,
    age_term,
    combine_terms,
    contribution_term,
    decayed,
    fair_share_term,
    rank_key,
    score_job,
    size_term,
)
from tests.scheduler.factories import NOW, job

# ── reference vectors (GPU requirements section 7.3; A-28, A-56) ───────

@pytest.mark.parametrize(
    "job_no, wait_min, gpus, walltime_h, f, c, expected",
    [
        ("#121", 80, 2, 12, 0.600000, 0.500000, "0.775833"),
        ("#118", 45, 2, 6, 0.375000, 0.900000, "0.573750"),
        ("#119", 12, 2, 3, 0.850000, 0.000000, "0.325625"),
    ],
)
def test_research_reference_vectors(job_no, wait_min, gpus, walltime_h, f, c, expected):   # A-28
    a = wait_min / 60
    s = size_term(gpus, walltime_h * 60)
    assert combine_terms(a, s, f, c) == Decimal(expected), job_no


@pytest.mark.parametrize(
    "job_no, wait_min, gpus, walltime_h, expected",
    [
        ("#301", 30, 1, 2, "0.196875"),
        ("#302", 12, 2, 8, "0.055000"),
        ("#303", 5, 1, 1, "0.031771"),
    ],
)
def test_course_reference_vectors(job_no, wait_min, gpus, walltime_h, expected):           # A-56
    a = wait_min / 60
    s = size_term(gpus, walltime_h * 60)
    assert combine_terms(a, s) == Decimal(expected), job_no


def test_score_job_end_to_end_matches_job_121():
    # F = 1 - 0.4/1.0 = 0.6; served == consumed gives C = 0.5
    usage = LabUsage(decayed_gpu_hours=0.4, served_gpu_hours=10, consumed_gpu_hours=10)
    b = score_job(job(gpus=2, walltime_h=12, waited_min=80), NOW, usage, total_decayed_gpu_hours=1.0)
    assert b.score == Decimal("0.775833")
    assert not b.escalated


# ── individual terms ───────────────────────────────────────────────────

def test_age_is_uncapped_hours():
    assert age_term(job(waited_min=600).submit_time, NOW) == 10.0


def test_fair_share_with_no_usage_anywhere_is_one():
    assert fair_share_term(0, 0) == 1.0


@pytest.mark.parametrize(
    "served, consumed, expected",
    [
        (0, 0, 0.5),       # new lab: neutral
        (5, 0, 1.0),       # pure lender
        (0, 5, 0.0),       # pure borrower
        (10, 10, 0.5),     # balanced
        (20, 10, 0.75),    # lends 2x
        (40, 10, 1.0),     # lends 4x: clamp
        (80, 10, 1.0),     # lends 8x: still clamped
        (10, 20, 0.25),
        (10, 80, 0.0),
    ],
)
def test_contribution_term(served, consumed, expected):
    assert contribution_term(served, consumed) == pytest.approx(expected)


def test_size_term_is_capped_at_one():
    assert size_term(2, 12 * 60) == 0.375
    assert size_term(4, 16 * 60) == 1.0
    assert size_term(8, 48 * 60) == 1.0


def test_course_score_ignores_fair_share_and_contribution():
    b = score_job(job(course=True, waited_min=30, gpus=1, walltime_h=2), NOW)
    assert b.fair_share is None and b.contribution is None
    assert b.score == Decimal("0.196875")


def test_usage_halves_every_half_life():
    week = 7 * 86400
    assert decayed(8.0, week) == pytest.approx(4.0)
    assert decayed(8.0, 2 * week) == pytest.approx(2.0)


# ── escalation and deterministic order (FR-GPU-050, 119) ──────────────

def test_job_waiting_t_max_escalates_ahead_of_any_score():
    old = job(job_id=1, waited_min=240, gpus=8, walltime_h=48)        # worst size, but escalated
    fresh = job(job_id=2, waited_min=0, gpus=1, walltime_h=1)
    keys = {j.job_id: rank_key(j, score_job(j, NOW)) for j in (old, fresh)}
    assert keys[1] < keys[2]


def test_escalated_jobs_are_ordered_oldest_first():
    a = job(job_id=1, waited_min=300)
    b = job(job_id=2, waited_min=500)
    ordered = sorted([a, b], key=lambda j: rank_key(j, score_job(j, NOW)))
    assert [j.job_id for j in ordered] == [2, 1]


def test_equal_scores_break_by_submit_time_then_job_id():                  # A-64
    same = [job(job_id=7, waited_min=30), job(job_id=3, waited_min=30), job(job_id=5, waited_min=31)]
    ordered = sorted(same, key=lambda j: rank_key(j, score_job(j, NOW)))
    assert [j.job_id for j in ordered] == [5, 3, 7]


# ── starvation bounds (NFR-GPU-009, 060), property tests ───────────────

terms = st.floats(min_value=0, max_value=1, allow_nan=False)
gpus = st.integers(min_value=1, max_value=8)
hours = st.integers(min_value=1, max_value=48)


@given(older_wait=st.integers(91, 230), gap=st.integers(91, 230), f1=terms, c1=terms, f2=terms, c2=terms,
       g1=gpus, g2=gpus, h1=hours, h2=hours)
def test_research_job_waiting_90_minutes_longer_always_ranks_first(older_wait, gap, f1, c1, f2, c2, g1, g2, h1, h2):
    younger_wait = max(0, older_wait - gap)
    gap = older_wait - younger_wait
    if gap <= DEFAULT_CONFIG.research_starvation_bound_minutes:
        return
    older = combine_terms(older_wait / 60, size_term(g1, h1 * 60), f1, c1)
    younger = combine_terms(younger_wait / 60, size_term(g2, h2 * 60), f2, c2)
    assert older > younger


@given(younger_wait=st.integers(0, 200), gap=st.integers(16, 200), g1=gpus, g2=gpus, h1=hours, h2=hours)
def test_course_job_waiting_15_minutes_longer_always_ranks_first(younger_wait, gap, g1, g2, h1, h2):
    older = combine_terms((younger_wait + gap) / 60, size_term(g1, h1 * 60))
    younger = combine_terms(younger_wait / 60, size_term(g2, h2 * 60))
    assert older > younger


# ── configuration (NFR-GPU-062) ─────────────────────────────────────────

def test_derived_bounds_match_the_specification():
    assert DEFAULT_CONFIG.research_starvation_bound_minutes == pytest.approx(90)
    assert DEFAULT_CONFIG.course_starvation_bound_minutes == pytest.approx(15)


def test_invalid_configuration_is_refused_naming_the_invariant():
    with pytest.raises(ConfigError, match="w_age > 0"):
        SchedulerConfig(w_age=0).validate()
    with pytest.raises(ConfigError, match="T_max < max_walltime"):
        SchedulerConfig(t_max_minutes=48 * 60).validate()
