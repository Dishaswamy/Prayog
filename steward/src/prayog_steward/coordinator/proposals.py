"""Turning a model's ProposalDraft into an exact, bounded job spec (FR-GPU-149, 150, 187).

The model suggests changes. The server applies them to the failed run's spec under hard
bounds and computes the requested GPU-hours. The owner sees exactly that diff before
approving, and approval goes through the ordinary submission path.
"""

from __future__ import annotations

import re
from dataclasses import replace

from prayog_steward.config import StewardSettings
from prayog_steward.ports import JobSpec
from steward_contracts import JobChange, JobField

_PIN = re.compile(r"^([A-Za-z][A-Za-z0-9_.\-]*)==(\d+(?:\.\d+){0,3}[\w.+\-]*)$")
PRECISIONS = {"fp32", "fp16", "bf16", "tf32", "fp8", "mixed"}
MAX_COMMAND_CHARS = 1_000


def _positive_int(value: str, upper: int) -> int | None:
    try:
        n = int(value.strip())
    except ValueError:
        return None
    return n if 1 <= n <= upper else None


def apply_change(spec: JobSpec, change: JobChange, settings: StewardSettings) -> JobSpec | None:
    """The new spec, or None if the change is invalid or out of bounds."""
    value = change.new_value.strip()
    match change.field:
        case JobField.ENV_PIN:
            m = _PIN.match(value)
            if not m:
                return None
            name = m.group(1).lower()
            pins = [p for p in spec.env_pins if not p.lower().startswith(f"{name}==")]
            return replace(spec, env_pins=(*pins, f"{m.group(1)}=={m.group(2)}"))
        case JobField.COMMAND_ARGS:
            if not value or len(value) > MAX_COMMAND_CHARS or "\n" in value:
                return None
            return replace(spec, command_args=value)
        case JobField.GPU_COUNT:
            n = _positive_int(value, settings.max_gpu_count)
            return replace(spec, gpu_count=n) if n else None
        case JobField.WALLTIME_MINUTES:
            n = _positive_int(value, settings.max_walltime_minutes)
            return replace(spec, walltime_minutes=n) if n else None
        case JobField.BATCH_SIZE:
            n = _positive_int(value, 1_000_000)
            return replace(spec, batch_size=n) if n else None
        case JobField.PRECISION:
            return replace(spec, precision=value.lower()) if value.lower() in PRECISIONS else None


def changeable_fields(settings: StewardSettings) -> list[JobField]:
    """The job fields the platform's submission path can change (settings.proposal_fields)."""
    if settings.proposal_fields is None:
        return list(JobField)
    return [f for f in JobField if f.value in set(settings.proposal_fields)]


def apply_changes(
    spec: JobSpec, changes: list[JobChange], settings: StewardSettings
) -> tuple[JobSpec, list[JobChange]]:
    """Apply valid changes in order. One change per field; invalid ones, and changes to
    fields the platform cannot submit, are dropped (an owner must never approve a
    change that would silently not happen)."""
    allowed = set(changeable_fields(settings))
    applied: list[JobChange] = []
    seen: set[JobField] = set()
    for change in changes:
        if change.field in seen or change.field not in allowed:
            continue
        new = apply_change(spec, change, settings)
        if new is None or new == spec:
            continue
        spec = new
        seen.add(change.field)
        applied.append(change)
    return spec, applied
