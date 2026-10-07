"""Fail if docs/traceability.md names a test that does not exist under tests/."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    named = set(re.findall(r"`(test_\w+)`", (ROOT / "docs/traceability.md").read_text("utf-8")))
    defined: set[str] = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        defined.update(re.findall(r"^def (test_\w+)", path.read_text("utf-8"), re.M))
    missing = sorted(named - defined)
    for name in missing:
        print(f"traceability.md names a missing test: {name}")
    print(f"{len(named)} tests traced, {len(missing)} missing")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
