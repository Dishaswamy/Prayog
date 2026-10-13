"""
Scheduler configuration (GPU requirements section 18).

Every policy constant is configuration, never a literal in the code
(NFR-GPU-053). Values that other requirements depend on are derived, never
configured, so a bound cannot drift out of step with the weights.
"""

from dataclasses import dataclass


class ConfigError(ValueError):
    """A configuration violates one of the invariants in section 18."""


@dataclass(frozen=True)
class SchedulerConfig:
    # Request limits
    min_walltime_minutes: int = 1
    max_walltime_minutes: int = 48 * 60
    max_active_jobs_per_user: int = 50
    idempotency_window_hours: int = 24

    # Score weights (FR-GPU-045, 182). The size weight is subtracted.
    w_age: float = 0.40
    w_fair: float = 0.30
    w_contrib: float = 0.20
    w_size: float = 0.10

    age_normaliser_minutes: float = 60.0
    t_max_minutes: int = 4 * 60               # escalation threshold (FR-GPU-050)
    usage_half_life_days: float = 7.0         # fair-share decay (FR-GPU-047)
    contribution_window_days: int = 30        # rolling window for C (FR-GPU-048)
    size_denominator_gpu_hours: float = 64.0  # FR-GPU-049
    score_decimal_places: int = 6             # FR-GPU-185

    # Timing
    scheduler_tick_seconds: float = 10.0
    t_inprocess_seconds: float = 5.0
    heartbeat_interval_seconds: float = 10.0
    heartbeat_timeout_seconds: float = 30.0

    # ── derived values (never configured) ─────────────────────────────
    @property
    def research_starvation_bound_minutes(self) -> float:
        """Waiting this much longer always wins within a class (NFR-GPU-009)."""
        return (self.w_fair + self.w_contrib + self.w_size) / self.w_age * self.age_normaliser_minutes

    @property
    def course_starvation_bound_minutes(self) -> float:
        """Same bound for course jobs, which have only the size term (NFR-GPU-060)."""
        return self.w_size / self.w_age * self.age_normaliser_minutes

    def validate(self) -> "SchedulerConfig":
        """Refuse a configuration that breaks an invariant (NFR-GPU-062)."""
        checks = [
            (self.w_age > 0, "w_age > 0: the starvation bounds are undefined if age carries no weight"),
            (self.min_walltime_minutes <= self.max_walltime_minutes, "min_walltime <= max_walltime"),
            (self.t_max_minutes < self.max_walltime_minutes, "T_max < max_walltime: escalation must be reachable"),
            (self.scheduler_tick_seconds > 0, "scheduler_tick > 0"),
            (self.heartbeat_timeout_seconds >= 3 * self.heartbeat_interval_seconds,
             "heartbeat_timeout >= 3 x heartbeat_interval: fewer missed beats makes DOWN detection flap"),
            (self.size_denominator_gpu_hours > 0, "size_denominator > 0"),
            (self.usage_half_life_days > 0, "usage_half_life > 0"),
        ]
        for ok, invariant in checks:
            if not ok:
                raise ConfigError(f"Invalid scheduler configuration, violates: {invariant}")
        return self


DEFAULT_CONFIG = SchedulerConfig().validate()
