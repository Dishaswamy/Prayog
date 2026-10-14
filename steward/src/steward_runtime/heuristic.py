"""HeuristicReasoning: a rule-based, offline baseline that obeys the same contracts.

Two uses:
- Development and demos without an API key (``STEWARD_REASONING=heuristic``).
- The evaluation baseline. The spec argues "a fixed rule cannot replace S1" (§11.6.1),
  and ``tools/evaluate.py`` measures Claude against exactly this.

It is deliberately simple: pattern-match the error, cite the record it matched, suggest
one conventional fix. It never invents a citation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from steward_contracts import (
    InvokeOutcome,
    InvokeRequest,
    InvokeResponse,
    Record,
    RecordKind,
    S1Input,
    S2Input,
    S3Input,
    S4Input,
)

AGENT_VERSION = "heuristic-1"

# (failure class, pattern, explanation)
RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "oom",
        re.compile(r"out of memory|OutOfMemoryError|CUDA error: out of memory|OOM", re.I),
        "The process ran out of GPU memory.",
    ),
    (
        "library_version",
        re.compile(
            r"ModuleNotFoundError|ImportError|cannot import name|has no attribute|"
            r"version conflict|ResolutionImpossible|undefined symbol",
            re.I,
        ),
        "A library is missing or has an incompatible version.",
    ),
    (
        "missing_data",
        re.compile(
            r"FileNotFoundError|No such file or directory|"
            r"dataset not found|EmptyDataError",
            re.I,
        ),
        "An input file or dataset could not be found.",
    ),
    (
        "hardware",
        re.compile(
            r"Xid|ECC error|uncorrectable|device-side assert.*lost|"
            r"GPU has fallen off the bus|NVML",
            re.I,
        ),
        "The error looks like a GPU or driver fault.",
    ),
    (
        "interruption",
        re.compile(r"Killed|SIGTERM|SIGKILL|KeyboardInterrupt|preempt", re.I),
        "The process was interrupted or killed.",
    ),
    (
        "code_bug",
        re.compile(r"Traceback \(most recent call last\)|Error:", re.I),
        "The training code raised an exception.",
    ),
]


def _match(records: Sequence[Record]) -> tuple[str, str, str | None]:
    logs = [r for r in records if r.kind is RecordKind.LOG_EXCERPT]
    for cls, pattern, why in RULES:
        for rec in logs:
            if pattern.search(rec.body):
                return cls, why, rec.record_id
    return "unknown", "No known error pattern was found in the log.", None


def _batch_size(records: Sequence[Record]) -> int | None:
    for rec in records:
        if rec.kind is RecordKind.RUN and (m := re.search(r'"batch_size": (\d+)', rec.body)):
            return int(m.group(1))
    return None


class HeuristicReasoning:
    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        payload = request.payload
        if isinstance(payload, S1Input):
            result = self._s1(payload)
        elif isinstance(payload, S2Input):
            result = self._s2(payload)
        elif isinstance(payload, S3Input):
            result = self._s3(payload)
        else:
            assert isinstance(payload, S4Input)
            result = self._s4(payload)
        return InvokeResponse(
            invocation_id=request.invocation_id,
            context=request.context,
            outcome=InvokeOutcome.OK,
            result=result,
            agent_id="prayog-steward",
            agent_version=AGENT_VERSION,
            model=None,
        )

    def _s1(self, p: S1Input) -> dict[str, Any]:
        cls, why, cite = _match(p.records)
        claims = [{"text": why, "cites": [cite]}] if cite else []
        earlier = [
            r.record_id
            for r in p.records
            if r.kind is RecordKind.EARLIER_RUN and '"end": "FAILED"' in r.body
        ][:3]
        proposal = None
        batch = _batch_size(p.records)
        if cls == "oom" and cite and batch and batch > 1:
            proposal = {
                "changes": [
                    {
                        "field": "batch_size",
                        "new_value": str(max(1, batch // 2)),
                        "reason": {
                            "text": "Halving the batch size usually fits the run in memory.",
                            "cites": [cite],
                        },
                    }
                ]
            }
        verdict = (
            "likely_hardware"
            if cls == "hardware"
            else ("inconclusive" if cls in ("unknown", "interruption") else "unlikely_hardware")
        )
        indicators = ["xid_error"] if cls == "hardware" else []
        return {
            "classification": cls,
            "claims": claims,
            "matching_earlier_failures": earlier,
            "hardware_finding": {"verdict": verdict, "indicators": indicators},
            "proposal": proposal,
        }

    def _s2(self, p: S2Input) -> dict[str, Any]:
        units = {r.unit_id for r in p.rows}
        categories = {r.failure_category for r in p.rows}
        same = len(units) == 1 and len(categories) <= 2
        ids = [r.row_id for r in p.rows]
        text = (
            f"{len(ids)} failures from {len({r.owner_lab for r in p.rows})} labs on "
            f"{', '.join(sorted(units))}"
        )
        return {
            "same_fault": same,
            "row_ids": ids if same else [],
            "finding": {
                "verdict": "likely_hardware" if same else "inconclusive",
                "indicators": ["repeated_on_same_unit"] if same else [],
            },
            "rationale": [{"text": text, "cites": ids[:5]}] if ids else [],
        }

    def _s3(self, p: S3Input) -> dict[str, Any]:
        cls, why, cite = _match(p.records)
        claims: list[dict[str, Any]] = []
        if cite:
            claims.append({"text": why, "provenance": "internal", "cites": [cite]})
        ledger = [r for r in p.records if r.kind is RecordKind.LEDGER]
        if ledger:
            claims.append(
                {
                    "text": "Your ledger records the environment this snapshot used.",
                    "provenance": "internal",
                    "cites": [ledger[0].record_id],
                }
            )
        for ref in [r for r in p.records if r.kind is RecordKind.REFERENCE][:2]:
            claims.append(
                {
                    "text": f"Related published work: {_title(ref)}.",
                    "provenance": "literature",
                    "cites": [ref.record_id],
                }
            )
        for ext in [r for r in p.records if r.kind is RecordKind.EXTERNAL][:1]:
            claims.append(
                {
                    "text": "A web source discusses this error.",
                    "provenance": "external",
                    "cites": [ext.record_id],
                }
            )
        if not claims:
            claims.append(
                {
                    "text": "No matching evidence; check the end of the log first.",
                    "provenance": "reasoning",
                    "cites": [],
                }
            )
        playbook: dict[str, list[tuple[str, str | None]]] = {
            "library_version": [
                (
                    "Recreate the environment from the snapshot's pinned versions.",
                    "pip install -r requirements.lock",
                ),
                ("Check the installed version of the failing package.", "pip show <package>"),
            ],
            "oom": [("Lower the batch size or enable mixed precision, then rebuild.", None)],
            "missing_data": [("Confirm the dataset path exists and is readable.", "ls -l <path>")],
        }
        steps = playbook.get(
            cls, [("Read the last traceback in the log and fix the raising line.", None)]
        )
        return {
            "claims": claims,
            "steps": [{"explanation": e, "command": c} for e, c in steps],
            "evidence_found": cite is not None or bool(ledger),
        }

    def _s4(self, p: S4Input) -> dict[str, Any]:
        metrics = [r for r in p.records if r.kind is RecordKind.METRICS]
        run = [r for r in p.records if r.kind is RecordKind.RUN]
        out: list[dict[str, Any]] = []
        if metrics:
            out.append(
                {
                    "kind": "hyperparameter",
                    "text": "Try a learning-rate sweep around the current value; compare "
                    "validation loss against your earlier runs.",
                    "cites": [metrics[0].record_id],
                }
            )
        if run:
            out.append(
                {
                    "kind": "training_setup",
                    "text": "If GPU utilisation is low, increase the data-loader workers "
                    "or the batch size.",
                    "cites": [run[0].record_id],
                }
            )
        for ref in [r for r in p.records if r.kind is RecordKind.REFERENCE][:2]:
            anchor = (metrics or run)[:1]
            out.append(
                {
                    "kind": "training_setup",
                    "text": f"Consider the guidance in '{_title(ref)}' for this configuration.",
                    "cites": [ref.record_id, *(a.record_id for a in anchor)],
                }
            )
        return {"suggestions": out}


def _title(record: Record) -> str:
    """Reference records start with the paper title on their first line."""
    return record.body.splitlines()[0].strip()[:160] if record.body else ""
