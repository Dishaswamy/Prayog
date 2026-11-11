"""Configuration from the environment, checked at startup (NFR-GPU-053, NFR-GPU-062; A-78, A-79)."""

import pytest

from logic.gpu.scheduler.config import DEFAULT_CONFIG, ConfigError, SchedulerConfig, load_config


def test_defaults_match_section_18():
    c = DEFAULT_CONFIG
    assert (c.stop_grace_seconds, c.release_confirm_seconds, c.walltime_tolerance_seconds) == (15, 10, 5)
    assert (c.dispatch_ack_timeout_seconds, c.max_dispatch_attempts, c.drain_grace_minutes) == (60, 3, 10)


def test_derived_bounds_follow_the_parameters():  # section 18, derived values
    c = DEFAULT_CONFIG
    assert c.walltime_completion_bound_seconds == 30
    assert c.node_down_detection_bound_seconds == 40
    assert c.dispatch_failure_bound_seconds == 180
    assert c.hard_drain_completion_bound_seconds == 625
    assert SchedulerConfig(stop_grace_seconds=20).walltime_completion_bound_seconds == 35


def test_no_environment_gives_the_defaults():
    assert load_config({}) == DEFAULT_CONFIG


def test_a_setting_comes_from_the_environment_without_code_changes():  # A-78, NFR-GPU-053
    cfg = load_config({"PRAYOG_SCHED_MAX_WALLTIME_MINUTES": "300", "PRAYOG_SCHED_W_SIZE": "0.2", "HOME": "x"})
    assert (cfg.max_walltime_minutes, cfg.w_size) == (300, 0.2)


@pytest.mark.parametrize("env, invariant", [
    ({"PRAYOG_SCHED_WALLTIME_TOLERANCE_SECONDS": "0"}, "walltime_tolerance > 0"),             # A-79
    ({"PRAYOG_SCHED_HEARTBEAT_TIMEOUT_SECONDS": "20"}, "heartbeat_timeout >= 3 x heartbeat_interval"),  # A-79
    ({"PRAYOG_SCHED_RELEASE_CONFIRM_SECONDS": "0"}, "walltime_tolerance > 0 and release_confirm > 0"),
    ({"PRAYOG_SCHED_WARNING_COOLDOWN_HOURS": "1"}, "warning_cooldown >= correlation_window"),
    ({"PRAYOG_SCHED_T_MAX_MINUTES": "3000"}, "T_max < max_walltime"),
    ({"PRAYOG_SCHED_MIN_WALLTIME_MINUTES": "3000"}, "min_walltime <= max_walltime"),
    ({"PRAYOG_SCHED_W_AGE": "0"}, "w_age > 0"),
    ({"PRAYOG_SCHED_SCHEDULER_TICK_SECONDS": "0"}, "scheduler_tick > 0"),
])
def test_startup_refuses_a_configuration_that_breaks_an_invariant(env, invariant):  # NFR-GPU-062, A-79
    with pytest.raises(ConfigError, match=f"violates: {invariant}"):
        load_config(env)


def test_a_misspelt_setting_is_refused_rather_than_ignored():
    with pytest.raises(ConfigError, match="Unknown scheduler setting PRAYOG_SCHED_MAX_WALLTIME"):
        load_config({"PRAYOG_SCHED_MAX_WALLTIME": "180"})


@pytest.mark.parametrize("key, raw, kind", [
    ("PRAYOG_SCHED_MAX_ACTIVE_JOBS_PER_USER", "fifty", "an integer"),
    ("PRAYOG_SCHED_W_AGE", "high", "a number"),
])
def test_a_value_that_is_not_a_number_is_refused(key, raw, kind):
    with pytest.raises(ConfigError, match=f"{key} must be {kind}"):
        load_config({key: raw})
