"""S1 evaluation (Project Document §10: "how we will judge it").

Runs labelled failure cases through the real S1 input path (sanitiser and Gateway
records) and a reasoning adapter, validates each answer with the server's validator, and
reports accuracy, a confusion matrix, citation stripping and proposal rate.

    python tools/evaluate.py                       # rule-based baseline only (free)
    python tools/evaluate.py --claude              # baseline and Claude (costs API credits)
    python tools/evaluate.py --cases eval/s1_cases.jsonl --out eval/report.md

Case format (JSONL, one per line):
    {"id": "...", "label": "oom", "log": "...", "env_pins": ["torch==2.3.1"],
     "batch_size": 128, "source": "synthetic" | "real"}

The seed file ships a handful of *synthetic* cases so the pipeline runs end to end.
The evaluation the project promised needs ~100 *real* failed runs labelled by hand.
Add them with "source": "real" and report those numbers, not the seed set's.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prayog_steward.audit import Auditor  # noqa: E402
from prayog_steward.config import StewardSettings  # noqa: E402
from prayog_steward.coordinator.validation import validate_response  # noqa: E402
from prayog_steward.fakes import FakeAuditSink, FakeClock, FakeLogStore, FakeRunReader  # noqa: E402
from prayog_steward.gateway import Gateway  # noqa: E402
from prayog_steward.ports import JobSpec, RunEnd, RunView  # noqa: E402
from prayog_steward.rag.sanitiser import Sanitiser  # noqa: E402
from steward_contracts import (  # noqa: E402
    FailureClass,
    InvokeRequest,
    ReasoningPort,
    S1Input,
    S1Result,
)


@dataclass
class Case:
    id: str
    label: str
    log: str
    env_pins: tuple[str, ...]
    batch_size: int | None
    source: str


def load(path: Path) -> list[Case]:
    cases = []
    for line in path.read_text("utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            cases.append(
                Case(
                    d["id"],
                    d["label"],
                    d["log"],
                    tuple(d.get("env_pins", [])),
                    d.get("batch_size"),
                    d.get("source", "synthetic"),
                )
            )
    return cases


def request_for(case: Case, gateway: Gateway, logs: FakeLogStore) -> InvokeRequest:
    now = datetime.now(UTC)
    run = RunView(
        run_id=case.id,
        job_id=f"job-{case.id}",
        project_id="eval",
        owner_id="eval",
        owner_lab="eval",
        node_id="n1",
        unit_ids=("n1/gpu0",),
        end=RunEnd.FAILED,
        started_at=now - timedelta(hours=1),
        ended_at=now,
        job_spec=JobSpec(
            "python train.py", case.env_pins, 1, 60, "sha256:eval", batch_size=case.batch_size
        ),
        exit_code=1,
        failure_category="CRASH",
        log_refs=(f"logs/{case.id}",),
    )
    logs.logs[f"logs/{case.id}"] = case.log
    records = [gateway.run_record(run), *gateway.log_records(run)]
    return InvokeRequest(
        invocation_id=f"eval-{case.id}", payload=S1Input(run_id=case.id, records=records)
    )


def evaluate(
    name: str, reasoning: ReasoningPort, cases: list[Case], gateway: Gateway, logs: FakeLogStore
) -> str:
    labels = [c.value for c in FailureClass]
    confusion: Counter[tuple[str, str]] = Counter()
    correct = stripped = proposals = failures = 0
    for case in cases:
        v = validate_response(
            request_for(case, gateway, logs), reasoning.invoke(request_for(case, gateway, logs))
        )
        if not isinstance(v.result, S1Result):
            failures += 1
            confusion[(case.label, f"<{v.outcome.value}>")] += 1
            continue
        predicted = v.result.classification.value
        confusion[(case.label, predicted)] += 1
        correct += predicted == case.label
        stripped += v.stripped
        proposals += v.result.proposal is not None
    n = len(cases)
    lines = [
        f"### {name}",
        "",
        f"- accuracy: **{correct}/{n} = {correct / n:.0%}**" if n else "- no cases",
        f"- no usable output: {failures}",
        f"- claims stripped as uncited: {stripped}",
        f"- runs with a proposal: {proposals}",
        "",
        "| true \\ predicted | " + " | ".join(labels) + " | other |",
        "|---|" + "---|" * (len(labels) + 1),
    ]
    for true in labels:
        row = [str(confusion[(true, p)] or "") for p in labels]
        other = sum(v for (t, p), v in confusion.items() if t == true and p not in labels)
        lines.append(f"| **{true}** | " + " | ".join(row) + f" | {other or ''} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(ROOT / "eval" / "s1_cases.jsonl"))
    parser.add_argument("--claude", action="store_true")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    cases = load(Path(args.cases))
    settings = StewardSettings(_env_file=None)  # type: ignore[call-arg]
    logs = FakeLogStore()
    gateway = Gateway(
        runs=FakeRunReader(),
        logs=logs,
        sanitiser=Sanitiser(),
        settings=settings,
        auditor=Auditor(FakeAuditSink(), FakeClock()),
    )
    from steward_runtime.heuristic import HeuristicReasoning

    sources = Counter(c.source for c in cases)
    report = [
        f"# S1 evaluation ({datetime.now(UTC):%Y-%m-%d})",
        "",
        f"{len(cases)} cases: " + ", ".join(f"{v} {k}" for k, v in sources.items()),
        "",
    ]
    if sources.get("synthetic"):
        report += [
            "> Synthetic seed cases only check that the pipeline works. "
            "They say nothing about real accuracy.",
            "",
        ]
    report.append(evaluate("Rule-based baseline", HeuristicReasoning(), cases, gateway, logs))
    if args.claude:
        from steward_runtime.claude import ClaudeReasoning

        report.append(evaluate("Claude", ClaudeReasoning(), cases, gateway, logs))
    text = "\n".join(report)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
    print(text)


if __name__ == "__main__":
    main()
