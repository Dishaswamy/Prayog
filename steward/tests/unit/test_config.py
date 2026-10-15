from datetime import timedelta

import pytest
from pydantic import ValidationError

from prayog_steward.config import StewardSettings


def test_defaults_match_integrated_spec_section_16() -> None:
    s = StewardSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.correlation_window == timedelta(hours=24)
    assert s.min_distinct_users_for_warning == 2
    assert s.warning_cooldown == timedelta(hours=24)
    assert s.max_evidence_bundle_rows == 50
    assert s.steward_link_ttl == timedelta(minutes=15)
    assert s.rag_retention_period == timedelta(days=90)


@pytest.mark.parametrize(
    "override",
    [
        {"steward_link_ttl": timedelta(0)},
        {"rag_retention_period": timedelta(0)},
        {"min_distinct_users_for_warning": 1},
        {"max_evidence_bundle_rows": 1},
    ],
)
def test_section_16_7_invariants_are_enforced(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        StewardSettings(_env_file=None, **override)  # type: ignore[call-arg]


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEWARD_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("STEWARD_STEWARD_LINK_TTL", "PT10M")
    s = StewardSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.search_provider == "tavily"
    assert s.steward_link_ttl.total_seconds() == 600


def test_env_example_loads_as_a_working_configuration() -> None:
    """Copying .env.example to .env must give a valid configuration (empty = default)."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / ".env.example"
    s = StewardSettings(_env_file=str(example))  # type: ignore[call-arg]
    assert s.cookie_secure is None and s.maintenance_database_url is None
    assert s.embedder == "hashing" and s.search_provider == "none"
    assert s.steward_link_ttl == timedelta(minutes=15)
