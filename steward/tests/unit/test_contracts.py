from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from factories import REQUESTS, s1_request
from steward_contracts import (
    RESULT_TYPES,
    Context,
    HardwareFinding,
    InvokeRequest,
    S1Result,
    StewardInput,
)


def _walk(schema: Any) -> list[dict[str, Any]]:
    """All object nodes in a JSON schema, including $defs."""
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found.extend(_walk(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_walk(value))
    return found


@pytest.mark.parametrize("ctx", list(Context))
def test_result_schemas_forbid_additional_properties(ctx: Context) -> None:
    """Structured outputs need closed objects; ADR-S04 relies on it to catch write attempts."""
    objects = _walk(RESULT_TYPES[ctx].model_json_schema())
    assert objects
    assert all(o.get("additionalProperties") is False for o in objects)


def test_extra_fields_are_rejected() -> None:
    good = {
        "classification": "oom",
        "claims": [],
        "matching_earlier_failures": [],
        "hardware_finding": {"verdict": "inconclusive", "indicators": []},
        "proposal": None,
    }
    S1Result.model_validate(good)
    with pytest.raises(ValidationError) as exc:
        S1Result.model_validate({**good, "action": {"type": "update_quota"}})
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_fr_gpu_166_hardware_finding_has_no_free_text() -> None:
    """The admin-facing finding is enums only, so it cannot carry project content."""
    schema = HardwareFinding.model_json_schema()
    props = schema["properties"]
    assert set(props) == {"verdict", "indicators"}
    with pytest.raises(ValidationError):
        HardwareFinding.model_validate(
            {"verdict": "likely_hardware", "indicators": ["OOM at batch 512 in train.py"]}
        )


@pytest.mark.parametrize("ctx", list(Context))
def test_inputs_round_trip_through_the_discriminated_union(ctx: Context) -> None:
    request = REQUESTS[ctx]()
    again = InvokeRequest.model_validate_json(request.model_dump_json())
    assert again == request
    assert again.context is ctx
    TypeAdapter(StewardInput).validate_python(request.payload.model_dump())


def test_payload_context_cannot_be_spoofed() -> None:
    data = s1_request().model_dump()
    data["payload"]["context"] = "S2"
    with pytest.raises(ValidationError):
        InvokeRequest.model_validate(data)
