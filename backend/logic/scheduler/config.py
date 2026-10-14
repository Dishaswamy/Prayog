"""
Scheduler configuration (GPU requirements section 18).

Every policy constant is configuration, never a literal in the code
(NFR-GPU-053). Values that other requirements depend on are derived, never
configured, so a bound cannot drift out of step with the weights.

Any field can be set from the environment as PRAYOG_SCHED_<FIELD NAME>, for example
PRAYOG_SCHED_MAX_WALLTIME_MINUTES=180. The configuration is checked against the
section 18 invariants when the API or the tick starts, and a violation stops
startup with a message naming the invariant (NFR-GPU-062).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace

ENV_PREFIX = "PRAYOG_SCHED_"


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
    stop_grace_seconds: float = 15.0          # SIGTERM to SIGKILL (FR-GPU-068)
    drain_grace_minutes: float = 10.0         # FR-GPU-080
    dispatch_ack_timeout_seconds: float = 60.0   # FR-GPU-126
    max_dispatch_attempts: int = 3            # FR-GPU-127
    walltime_tolerance_seconds: float = 5.0   # time to notice walltime and begin stopping (FR-GPU-074)
    release_confirm_seconds: float = 10.0     # confirming process exit and unmount (FR-GPU-069, 074)

    # Steward evidence (FR-GPU-158 to 165)
    correlation_window_hours: float = 24.0
    min_distinct_users_for_warning: int = 2
    warning_cooldown_hours: float = 24.0
    max_evidence_bundle_rows: int = 50

    # ── derived values (never configured) ─────────────────────────────
    @property
    def research_starvation_bound_minutes(self) -> float:
        """Waiting this much longer always wins within a class (NFR-GPU-009)."""
        return (self.w_fair + self.w_contrib + self.w_size) / self.w_age * self.age_normaliser_minutes

    @property
    def course_starvation_bound_minutes(self) -> float:
        """Same bound for course jobs, which have only the size term (NFR-GPU-060)."""
        return self.w_size / self.w_age * self.age_normaliser_minutes

    @property
    def walltime_completion_bound_seconds(self) -> float:
        """From walltime to units released: notice, stop, confirm."""
        return self.walltime_tolerance_seconds + self.stop_grace_seconds + self.release_confirm_seconds

    @property
    def node_down_detection_bound_seconds(self) -> float:
        return self.heartbeat_timeout_seconds + self.scheduler_tick_seconds

    @property
    def dispatch_failure_bound_seconds(self) -> float:
        return self.dispatch_ack_timeout_seconds * self.max_dispatch_attempts

    @property
    def hard_drain_completion_bound_seconds(self) -> float:
        return self.drain_grace_minutes * 60 + self.stop_grace_seconds + self.release_confirm_seconds

    def validate(self) -> "SchedulerConfig":
        """Refuse a configuration that breaks an invariant (NFR-GPU-062)."""
        checks = [
            (self.walltime_tolerance_seconds > 0 and self.release_confirm_seconds > 0,
             "walltime_tolerance > 0 and release_confirm > 0: the walltime completion bound must exceed stop_grace"),
            (self.heartbeat_timeout_seconds >= 3 * self.heartbeat_interval_seconds,
             "heartbeat_timeout >= 3 x heartbeat_interval: fewer missed beats makes DOWN detection flap"),
            (self.warning_cooldown_hours >= self.correlation_window_hours,
             "warning_cooldown >= correlation_window: a dismissed signature must not re-correlate early"),
            (self.t_max_minutes < self.max_walltime_minutes, "T_max < max_walltime: escalation must be reachable"),
            (self.min_walltime_minutes <= self.max_walltime_minutes, "min_walltime <= max_walltime"),
            (self.w_age > 0, "w_age > 0: the starvation bounds are undefined if age carries no weight"),
            (self.scheduler_tick_seconds > 0, "scheduler_tick > 0"),
            (self.min_walltime_minutes >= 1, "min_walltime >= 1 minute"),
            (self.size_denominator_gpu_hours > 0, "size_denominator > 0"),
            (self.usage_half_life_days > 0, "usage_half_life > 0"),
            (self.max_active_jobs_per_user >= 1, "max_active_jobs_per_user >= 1"),
        ]
        for ok, invariant in checks:
            if not ok:
                raise ConfigError(f"Invalid scheduler configuration, violates: {invariant}")
        return self


DEFAULT_CONFIG = SchedulerConfig().validate()


def load_config(environ: Mapping[str, str] | None = None) -> SchedulerConfig:
    """
    The defaults, overridden by PRAYOG_SCHED_* variables, then validated. An unknown
    variable or a value that is not a number is refused too, so a typo cannot
    silently leave a default in place.
    """
    environ = os.environ if environ is None else environ
    types = {f.name: f.type for f in fields(SchedulerConfig)}
    overrides = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX):].lower()
        if name not in types:
            raise ConfigError(f"Unknown scheduler setting {key}.")
        try:
            overrides[name] = types[name](raw)
        except ValueError:
            raise ConfigError(f"{key} must be a{'n integer' if types[name] is int else ' number'}, got {raw!r}.") from None
    return replace(DEFAULT_CONFIG, **overrides).validate()
