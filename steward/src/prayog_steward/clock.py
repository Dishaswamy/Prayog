"""Business time comes from a Clock, never from the database's now(), so TTLs, windows
and cooldowns are testable and consistent across API and Worker processes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
