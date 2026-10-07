import os
import sys
from pathlib import Path

import pytest

# Make tests/factories.py importable as `factories`.
sys.path.insert(0, str(Path(__file__).parent))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("STEWARD_TEST_DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="set STEWARD_TEST_DATABASE_URL to run Postgres tests")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)
