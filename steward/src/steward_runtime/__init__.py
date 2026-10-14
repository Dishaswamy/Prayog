"""Steward Runtime: reasons over scoped, sanitised input and returns structured results.

It has no database access, no write path and imports no server code (ADR-S01).
"""

from steward_runtime.stub import StubBehaviour, StubReasoning

__version__ = "0.1.0"
AGENT_ID = "prayog-steward"

__all__ = ["AGENT_ID", "StubBehaviour", "StubReasoning", "__version__"]
