# ADR-S05 — S2 is advisory; evidence rows gain a sanitised failure signature

**Status:** Accepted (2026-10-07). The signature field is a **change request (CR-S1)** to the Integrated Spec · **Closes:** CONTEXT §8 gap 3 · **Requirements:** FR-GPU-162–165, NFR-GPU-056, SADD ADR-21

## Context
The Project Document justifies S2 by the fact that "failure text differs in wording", a similarity judgement. But the evidence bundle may contain no log bodies (NFR-GPU-056), so S2 sees only exit codes and failure categories.

## Decision
- As in SADD ADR-21, the **server** raises the warning at the threshold, and S2's result is advisory only.
- Each evidence row may carry `failure_signature`: a normalised exception class plus a top-frame *library* module (for example `torch.cuda.OutOfMemoryError@torch.cuda.memory`). It is computed by the sanitiser and contains no user paths, values, code or project names.
- CR-S1 asks the spec owners to accept this field under NFR-GPU-056. If it is rejected, the field stays null and S2 works on categories alone.
