import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
# Make tests/factories.py importable as `factories`.
sys.path.insert(0, str(HERE))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Postgres tests (everything under tests/integration, or marked) need a database."""
    if os.environ.get("STEWARD_TEST_DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="set STEWARD_TEST_DATABASE_URL to run Postgres tests")
    integration = HERE / "integration"
    for item in items:
        if "postgres" in item.keywords or integration in Path(str(item.fspath)).parents:
            item.add_marker(skip)
