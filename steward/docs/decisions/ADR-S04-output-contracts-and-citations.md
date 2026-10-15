# ADR-S04 — Output contracts, citation validation and enum-only hardware findings

**Status:** Accepted (2026-10-07) · **Closes:** CONTEXT §8 gap 4 · **Requirements:** FR-GPU-161, 166, 187, FR-STW-017, 018, NFR-GPU-045, NFR-AUTH-18, NFR-INT-05

## Decision
- All Runtime outputs are Pydantic models in `steward_contracts` with `extra="forbid"`. A response carrying unknown fields (for example an attempted "action") is rejected as malformed and audited. That is how a "write-attempting response" (NFR-INT-05) is detected.
- Narrative content is a list of `Claim{text, cites[]}`. Every record handed to the model has a `record_id`. The server's citation validator **drops every claim that cites nothing or cites an ID outside the invocation's scope** before anything is stored or displayed (FR-GPU-161). S3 also allows `provenance="reasoning"` claims, which carry no citation but are labelled as general reasoning (FR-STW-018).
- The **hardware finding** that reaches the admin is structured only: a verdict enum plus indicator enums, with **no free text**. It therefore cannot leak a job's configuration to the admin (FR-GPU-166).
- A **proposal** from the model is a list of `JobChange` items over an allow-listed set of job fields. The server, not the model, computes the exact diff and the requested GPU-hours shown to the owner (FR-GPU-187).
- `refusal` stop reasons, schema failures and timeouts all map to "no usable output". The run stays sealed and scheduling is unaffected (NFR-GPU-055).
