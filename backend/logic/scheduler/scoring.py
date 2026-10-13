"""
Ranking inside a class (GPU requirements section 7.3).

    research (P0-P2):  score = 0.40 A + 0.30 F + 0.20 C - 0.10 S
    course   (P3):     score = 0.40 A - 0.10 S

    A  age            wait_minutes / 60, uncapped
    F  fair share     1 - lab decayed usage / total decayed usage   (0 usage -> share 0)
    C  contribution   clamp(log2(served / consumed), -2, +2) / 4 + 0.5 over a rolling window
    S  size           min(gpus x walltime_hours / 64, 1)

A job that has waited at least T_max skips scoring and goes to the head of its
class (FR-GPU-050). Scores are rounded to 6 decimal places and ranking uses the
rounded value (FR-GPU-185); ties break by earlier submit time, then lower job
ID (FR-GPU-119).
"""

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from logic.scheduler.config import DEFAULT_CONFIG, SchedulerConfig
from logic.scheduler.domain import AccountType, JobView


@dataclass(frozen=True)
class LabUsage:
    """What the scheduler knows about a lab's usage when a pass starts."""
    decayed_gpu_hours: float = 0.0   # this lab's exponentially decayed usage
    served_gpu_hours: float = 0.0    # other labs' runs on this lab's nodes, in the window
    consumed_gpu_hours: float = 0.0  # this lab's runs on other labs' nodes, in the window


NO_USAGE = LabUsage()


@dataclass(frozen=True)
class ScoreBreakdown:
    """Every input of a decision, so the recorded score is reproducible (FR-GPU-098)."""
    age: float
    fair_share: float | None         # None for course accounts (not applied)
    contribution: float | None
    size: float
    score: Decimal                   # rounded to score_decimal_places
    escalated: bool


# ── individual terms ───────────────────────────────────────────────────

def age_term(submit_time: datetime, now: datetime, cfg: SchedulerConfig = DEFAULT_CONFIG) -> float:
    waited_minutes = max(0.0, (now - submit_time).total_seconds() / 60)
    return waited_minutes / cfg.age_normaliser_minutes


def fair_share_term(lab_decayed: float, total_decayed: float) -> float:
    share = 0.0 if total_decayed <= 0 else lab_decayed / total_decayed
    return 1.0 - share


def contribution_term(served: float, consumed: float) -> float:
    if served <= 0 and consumed <= 0:
        return 0.5                 # new or idle lab: neutral
    if consumed <= 0:
        return 1.0                 # pure lender
    if served <= 0:
        return 0.0                 # pure borrower
    ratio = math.log2(served / consumed)
    return max(-2.0, min(2.0, ratio)) / 4 + 0.5


def size_term(gpus: int, walltime_minutes: int, cfg: SchedulerConfig = DEFAULT_CONFIG) -> float:
    return min(gpus * (walltime_minutes / 60) / cfg.size_denominator_gpu_hours, 1.0)


def decayed(value: float, elapsed_seconds: float, cfg: SchedulerConfig = DEFAULT_CONFIG) -> float:
    """Exponential decay with the usage half-life. Exact: decay is memoryless."""
    half_life_seconds = cfg.usage_half_life_days * 86400
    return value * 2 ** (-max(0.0, elapsed_seconds) / half_life_seconds)


def _round(x: float, places: int) -> Decimal:
    # Decimal from the float's shortest repr, then half-up: a total order with no
    # binary-rounding surprises, so tests compare 6-dp literals exactly.
    return Decimal(repr(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


# ── the score ──────────────────────────────────────────────────────────

def combine_terms(
    a: float,
    s: float,
    f: float | None = None,
    c: float | None = None,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> Decimal:
    """Weighted sum, rounded. Course accounts pass f = c = None (FR-GPU-182)."""
    if f is None or c is None:
        raw = cfg.w_age * a - cfg.w_size * s
    else:
        raw = cfg.w_age * a + cfg.w_fair * f + cfg.w_contrib * c - cfg.w_size * s
    return _round(raw, cfg.score_decimal_places)


def score_job(
    job: JobView,
    now: datetime,
    lab: LabUsage = NO_USAGE,
    total_decayed_gpu_hours: float = 0.0,
    cfg: SchedulerConfig = DEFAULT_CONFIG,
) -> ScoreBreakdown:
    a = age_term(job.submit_time, now, cfg)
    s = size_term(job.gpus, job.walltime_minutes, cfg)
    escalated = a * cfg.age_normaliser_minutes >= cfg.t_max_minutes

    if job.account_type == AccountType.COURSE:
        f = c = None
    else:
        f = fair_share_term(lab.decayed_gpu_hours, total_decayed_gpu_hours)
        c = contribution_term(lab.served_gpu_hours, lab.consumed_gpu_hours)

    return ScoreBreakdown(a, f, c, s, combine_terms(a, s, f, c, cfg), escalated)


def rank_key(job: JobView, breakdown: ScoreBreakdown) -> tuple:
    """
    Sort key within a class: escalated jobs first, oldest first among them;
    then highest rounded score; ties by earlier submit time, then lower job ID.
    """
    if breakdown.escalated:
        return (0, job.submit_time, job.job_id)
    return (1, -breakdown.score, job.submit_time, job.job_id)
