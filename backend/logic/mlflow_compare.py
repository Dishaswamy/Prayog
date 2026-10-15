from math import isfinite

from logic.errors import BadRequestError, ConflictError
from logic.mlflow import _owned_rebuild, _owned_snapshot
from mlflow_models import RunMetric


def compare_rebuild_metrics(db, rebuild_id: int, user, tolerances: dict[str, float]) -> dict:
    """Compare source-run metrics with a successful rebuild using absolute tolerances."""
    rebuild = _owned_rebuild(db, rebuild_id, user)
    if rebuild.state != "REBUILD_SUCCESS":
        raise ConflictError("Metrics can only be compared after a successful rebuild")

    snapshot = _owned_snapshot(db, rebuild.snapshot_id, user)
    if snapshot.state != "RECORDED":
        raise ConflictError("Only recorded snapshots can be compared")
    if not isinstance(tolerances, dict):
        raise BadRequestError("Metric tolerances must be a mapping")

    normalized = {}
    for name, tolerance in tolerances.items():
        if not isinstance(name, str) or not name.strip():
            raise BadRequestError("Tolerance metric names must not be blank")
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
            raise BadRequestError("Metric tolerances must be numeric and non-negative")
        if not isfinite(tolerance) or tolerance < 0:
            raise BadRequestError("Metric tolerances must be finite and non-negative")
        normalized[name.strip()] = float(tolerance)

    source = db.query(RunMetric).filter(
        RunMetric.run_id == snapshot.run_id,
        RunMetric.user_id == user.id,
    ).all()
    rebuilt = db.query(RunMetric).filter(
        RunMetric.run_id == rebuild.execution_run_id,
        RunMetric.user_id == user.id,
    ).all()

    source_values = {(metric.name, metric.step): metric.value for metric in source}
    rebuilt_values = {(metric.name, metric.step): metric.value for metric in rebuilt}
    keys = sorted(set(source_values) | set(rebuilt_values))
    results = []
    passed = bool(keys)

    for name, step in keys:
        expected = source_values.get((name, step))
        actual = rebuilt_values.get((name, step))
        tolerance = normalized.get(name, 0.0)
        difference = abs(actual - expected) if expected is not None and actual is not None else None
        metric_passed = difference is not None and difference <= tolerance
        if not metric_passed:
            passed = False
        results.append({
            "name": name,
            "step": step,
            "expected": expected,
            "actual": actual,
            "absolute_difference": difference,
            "tolerance": tolerance,
            "passed": metric_passed,
        })

    return {
        "rebuild_id": rebuild.id,
        "snapshot_id": snapshot.snapshot_id,
        "source_run_id": snapshot.run_id,
        "rebuild_run_id": rebuild.execution_run_id,
        "passed": passed,
        "metrics_compared": len(results),
        "metrics": results,
    }
