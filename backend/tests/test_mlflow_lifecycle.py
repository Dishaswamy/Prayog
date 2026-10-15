import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from logic.errors import BadRequestError, ConflictError, NotFoundError
from logic.mlflow import create_experiment, create_rebuild, create_run, finish_run, log_metric, start_run, update_rebuild
from mlflow_models import ModelSnapshot


class MLFlowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.user = SimpleNamespace(id=7)

    def set_owned_item(self, item):
        query = self.db.query.return_value.filter.return_value
        query.first.return_value = item
        query.with_for_update.return_value.first.return_value = item

    def running_run(self):
        return SimpleNamespace(
            id=3, experiment_id=2, project_id="project-1", model_id="model-1", user_id=7, run_type="FIRST_TIME",
            status="RUNNING", environment={"python": "3.12"},
            hyperparameters={"lr": 0.01}, code_snapshot_uri=None,
            code_snapshot_sha256=None, weights_uri=None, logs_uri=None,
            code_artifact_id=None, weights_artifact_id=None, logs_artifact_id=None,
            error_summary=None, finished_at=None, worker_id=None, worker_claimed_at=None,
        )

    def test_log_metric_rejects_blank_or_overlong_names(self):
        for name in ("   ", "m" * 101):
            with self.subTest(name_length=len(name)):
                self.db.reset_mock()
                self.set_owned_item(self.running_run())
                with self.assertRaises(BadRequestError):
                    log_metric(self.db, 3, self.user, name, 1.0, 0)
                self.db.add.assert_not_called()
                self.db.commit.assert_not_called()

    def test_log_metric_rejects_invalid_steps_and_nonfinite_values(self):
        for value, step in ((1.0, -1), (1.0, True), (float("nan"), 0), (float("inf"), 0)):
            with self.subTest(value=value, step=step):
                self.db.reset_mock()
                self.set_owned_item(self.running_run())
                with self.assertRaises(BadRequestError):
                    log_metric(self.db, 3, self.user, "accuracy", value, step)
                self.db.add.assert_not_called()
                self.db.commit.assert_not_called()

    def test_log_metric_normalizes_name_before_persisting(self):
        self.set_owned_item(self.running_run())
        result = log_metric(self.db, 3, self.user, " accuracy ", 0.95, 1)
        self.assertEqual(result.name, "accuracy")
        self.assertEqual(result.step, 1)
        self.db.commit.assert_called_once()

    def test_experiment_rejects_model_outside_owned_project(self):
        project = SimpleNamespace(project_id="project-1", user_id=7)
        project_query = MagicMock()
        project_query.filter.return_value.first.return_value = project
        model_query = MagicMock()
        model_query.filter.return_value.first.return_value = None
        self.db.query.side_effect = [project_query, model_query]

        with self.assertRaises(NotFoundError):
            create_experiment(
                self.db, self.user, "project-1", "foreign-model",
                "trial", None, {},
            )

        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_new_run_preserves_project_and_model_mapping(self):
        experiment = SimpleNamespace(
            id=2, project_id="project-1", model_id="model-1", user_id=7
        )
        self.db.query.return_value.filter.return_value.first.return_value = experiment

        result = create_run(
            self.db, 2, self.user, {"lr": 0.01}, {"python": "3.12"}
        )

        created = self.db.add.call_args.args[0]
        self.assertEqual(created.project_id, "project-1")
        self.assertEqual(created.model_id, "model-1")
        self.assertEqual(created.run_type, "FIRST_TIME")
        self.assertEqual(created.status, "CREATED")
        self.assertIs(result, created)

    def test_start_run_moves_created_to_running(self):
        run = SimpleNamespace(id=3, user_id=7, status="CREATED", started_at=None, environment={})
        self.set_owned_item(run)

        result = start_run(self.db, 3, self.user, {"python": "3.12"})

        self.assertIs(result, run)
        self.assertEqual(run.status, "RUNNING")
        self.assertIsInstance(run.started_at, datetime)
        self.assertEqual(run.environment, {"python": "3.12"})
        self.db.commit.assert_called_once()

    @patch("logic.mlflow.get_verified_artifact")
    def test_start_run_attaches_verified_code_artifact_for_worker_execution(self, verify):
        run = SimpleNamespace(
            id=3, user_id=7, experiment_id=2, status="CREATED", started_at=None,
            environment={}, code_artifact_id=None, code_snapshot_uri=None,
            code_snapshot_sha256=None, weights_artifact_id=None, weights_uri=None,
        )
        self.set_owned_item(run)
        artifact = SimpleNamespace(id="code-1", sha256="d" * 64)
        verify.return_value = (artifact, Path("/private/code.zip"))

        result = start_run(self.db, 3, self.user, {"entrypoint": "main.py"}, "code-1")

        self.assertIs(result, run)
        self.assertEqual(run.code_artifact_id, "code-1")
        self.assertEqual(run.code_snapshot_sha256, "d" * 64)
        self.assertEqual(run.code_snapshot_uri, "artifact:code-1")
        self.db.commit.assert_called_once()

    def test_user_cannot_finish_a_run_queued_for_worker_execution(self):
        run = self.running_run()
        run.code_artifact_id = "code-1"
        run.code_snapshot_sha256 = "d" * 64
        self.set_owned_item(run)

        with self.assertRaisesRegex(ConflictError, "queued for worker execution"):
            finish_run(self.db, 3, self.user, {"status": "SUCCEEDED"})

        self.db.commit.assert_not_called()

    def test_start_run_rejects_non_created_state(self):
        run = SimpleNamespace(id=3, user_id=7, status="SUCCEEDED")
        self.set_owned_item(run)

        with self.assertRaises(ConflictError):
            start_run(self.db, 3, self.user, None)

        self.db.commit.assert_not_called()

    @patch("logic.mlflow.get_verified_artifact")
    def test_successful_run_creates_immutable_snapshot(self, verify):
        run = self.running_run()
        self.set_owned_item(run)
        code = SimpleNamespace(id="artifact-code", sha256="a" * 64)
        weights = SimpleNamespace(id="artifact-weights", sha256="b" * 64)
        verify.side_effect = [(code, Path("/private/code.zip")), (weights, Path("/private/weights.bin"))]

        result = finish_run(self.db, 3, self.user, {
            "status": "SUCCEEDED",
            "code_artifact_id": code.id,
            "weights_artifact_id": weights.id,
            "logs_artifact_id": None,
            "error_summary": None,
        })

        snapshot = next(call.args[0] for call in self.db.add.call_args_list if isinstance(call.args[0], ModelSnapshot))
        self.assertEqual(snapshot.state, "RECORDED")
        self.assertEqual(snapshot.project_id, "project-1")
        self.assertEqual(snapshot.model_id, "model-1")
        self.assertEqual(snapshot.code_artifact_id, code.id)
        self.assertEqual(snapshot.weights_artifact_id, weights.id)
        self.assertEqual(snapshot.code_sha256, "a" * 64)
        self.assertEqual(run.status, "SUCCEEDED")
        self.assertIsNotNone(run.finished_at)
        self.assertIs(result, run)
        self.db.commit.assert_called_once()

    def test_successful_run_with_missing_code_persists_incomplete_snapshot(self):
        run = self.running_run()
        self.set_owned_item(run)

        result = finish_run(self.db, 3, self.user, {"status": "SUCCEEDED"})

        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.logging_state, "DEGRADED")
        self.assertIn("No code snapshot artifact was supplied.", result.logging_warnings)
        snapshot = self.db.add.call_args.args[0]
        self.assertEqual(snapshot.state, "INCOMPLETE")
        self.assertIsNone(snapshot.code_artifact_id)
        self.assertIn("No code snapshot artifact was supplied.", snapshot.completeness_reasons)
        self.db.commit.assert_called_once()

    @patch("logic.mlflow.get_verified_artifact")
    def test_failed_run_can_preserve_available_weights_in_incomplete_snapshot(self, verify):
        run = self.running_run()
        self.set_owned_item(run)
        weights = SimpleNamespace(id="weights-1", sha256="b" * 64)
        verify.return_value = (weights, Path("/private/weights.bin"))

        result = finish_run(self.db, 3, self.user, {
            "status": "FAILED",
            "weights_artifact_id": "weights-1",
            "error_summary": "training failed",
        })

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.weights_artifact_id, "weights-1")
        self.assertEqual(result.logging_state, "DEGRADED")
        snapshot = self.db.add.call_args.args[0]
        self.assertEqual(snapshot.state, "INCOMPLETE")
        self.assertEqual(snapshot.weights_artifact_id, "weights-1")
        self.db.commit.assert_called_once()

    @patch("logic.mlflow.get_verified_artifact")
    def test_failed_first_time_run_does_not_require_steward(self, verify):
        run = self.running_run()
        self.set_owned_item(run)

        finish_run(self.db, 3, self.user, {
            "status": "FAILED",
            "error_summary": "dependency import failed",
        })

        self.assertEqual(run.status, "FAILED")
        self.assertIsNotNone(run.finished_at)
        snapshot = self.db.add.call_args.args[0]
        self.assertEqual(snapshot.state, "INCOMPLETE")
        verify.assert_not_called()


    @patch("logic.mlflow.get_verified_artifact")
    def test_malformed_expected_metrics_make_snapshot_incomplete(self, verify):
        run = self.running_run()
        run.environment = {"python": "3.12", "expected_metrics": "accuracy"}
        self.set_owned_item(run)
        code = SimpleNamespace(id="code-1", sha256="a" * 64)
        verify.return_value = (code, Path("/private/code.zip"))

        result = finish_run(self.db, 3, self.user, {
            "status": "SUCCEEDED", "code_artifact_id": "code-1",
        })

        snapshot = self.db.add.call_args.args[0]
        self.assertEqual(result.logging_state, "DEGRADED")
        self.assertEqual(snapshot.state, "INCOMPLETE")
        self.assertTrue(any("Invalid expected_metrics declaration" in reason for reason in snapshot.completeness_reasons))
        self.db.commit.assert_called_once()

    def test_missing_expected_metrics_make_snapshot_incomplete(self):
        run = self.running_run()
        run.environment = {"python": "3.12", "expected_metrics": ["loss", "accuracy"]}
        self.set_owned_item(run)
        metric_query = MagicMock()
        metric_query.filter.return_value = metric_query
        metric_query.distinct.return_value = metric_query
        metric_query.all.return_value = [("loss",)]
        self.db.query.side_effect = [self.db.query.return_value, metric_query]

        result = finish_run(self.db, 3, self.user, {"status": "SUCCEEDED", "code_artifact_id": None})

        self.assertEqual(result.logging_state, "DEGRADED")
        snapshot = self.db.add.call_args.args[0]
        self.assertEqual(snapshot.state, "INCOMPLETE")
        self.assertTrue(any("accuracy" in reason for reason in snapshot.completeness_reasons))

    def test_rebuild_terminal_state_cannot_be_changed(self):
        attempt = SimpleNamespace(id=11, user_id=7, state="REBUILD_FAILED")
        self.set_owned_item(attempt)

        with self.assertRaises(ConflictError):
            update_rebuild(self.db, 11, self.user, "REBUILD_SUCCESS", None, None, None)

        self.db.commit.assert_not_called()

    @patch("logic.mlflow.get_verified_artifact")
    def test_successful_rebuild_finishes_execution_and_records_new_snapshot(self, verify):
        attempt = SimpleNamespace(
            id=11, user_id=7, snapshot_id="snapshot-source", execution_run_id=19,
            state="REBUILD_INITIATED", environment={"python": "3.12"},
            finished_at=None, logs_artifact_id=None, logs_uri=None, failure_summary=None,
        )
        source = SimpleNamespace(
            snapshot_id="snapshot-source", experiment_id=2, project_id="project-1", model_id="model-1", user_id=7,
            state="RECORDED", code_artifact_id="code-1", weights_artifact_id=None, code_sha256="c" * 64,
            dependencies={"python": "3.11"}, hyperparameters={"lr": 0.02},
        )
        execution = SimpleNamespace(
            id=19, user_id=7, status="RUNNING", hyperparameters={"lr": 0.02},
            environment={"python": "3.12"}, finished_at=None, logs_artifact_id=None,
            logs_uri=None, error_summary=None, code_artifact_id=None,
            code_snapshot_uri=None, code_snapshot_sha256=None,
            weights_artifact_id=None, weights_uri=None,
        )
        queries = []
        for item in (attempt, source, execution):
            query = MagicMock()
            query.filter.return_value = query
            query.with_for_update.return_value = query
            query.first.return_value = item
            queries.append(query)
        self.db.query.side_effect = queries
        code = SimpleNamespace(id="code-1", sha256="c" * 64)
        verify.return_value = (code, Path("/private/code.zip"))

        result = update_rebuild(
            self.db, 11, self.user, "REBUILD_SUCCESS", None, None, None
        )

        self.assertIs(result, attempt)
        self.assertEqual(attempt.state, "REBUILD_SUCCESS")
        self.assertEqual(execution.status, "SUCCEEDED")
        self.assertIsNotNone(execution.finished_at)
        self.assertEqual(self.db.add.call_count, 2)
        snapshot = next(
            call.args[0] for call in self.db.add.call_args_list
            if getattr(call.args[0], "state", None) == "RECORDED"
            and getattr(call.args[0], "run_id", None) == execution.id
        )
        event = next(
            call.args[0] for call in self.db.add.call_args_list
            if getattr(call.args[0], "event_type", None) == "OPTIMIZATION_REQUESTED"
        )
        self.assertEqual(snapshot.run_id, execution.id)
        self.assertEqual(snapshot.code_artifact_id, "code-1")
        self.assertIsNone(snapshot.weights_artifact_id)
        self.assertEqual(event.payload["terminal_state"], "REBUILD_SUCCESS")
        self.db.commit.assert_called_once()

    @patch("logic.mlflow.get_verified_artifact")
    def test_failed_rebuild_with_verified_log_atomically_enqueues_steward_event(self, verify):
        attempt = SimpleNamespace(
            id=11, user_id=7, snapshot_id="snapshot-source", execution_run_id=19,
            state="REBUILD_INITIATED", environment={"python": "3.12"},
            finished_at=None, logs_artifact_id=None, logs_uri=None, failure_summary=None,
        )
        source = SimpleNamespace(
            snapshot_id="snapshot-source", experiment_id=2, project_id="project-1",
            model_id="model-1", user_id=7, state="RECORDED", code_artifact_id="code-1",
            weights_artifact_id=None, code_sha256="c" * 64,
            dependencies={"python": "3.11"}, hyperparameters={"lr": 0.02},
        )
        execution = SimpleNamespace(
            id=19, user_id=7, status="RUNNING", hyperparameters={"lr": 0.02},
            environment={"python": "3.12"}, finished_at=None, logs_artifact_id=None,
            logs_uri=None, error_summary=None, code_artifact_id=None,
            code_snapshot_uri=None, code_snapshot_sha256=None,
            weights_artifact_id=None, weights_uri=None,
        )
        queries = []
        for item in (attempt, source, execution):
            query = MagicMock()
            query.filter.return_value = query
            query.with_for_update.return_value = query
            query.first.return_value = item
            queries.append(query)
        self.db.query.side_effect = queries
        log = SimpleNamespace(id="log-verified", sha256="a" * 64)
        code = SimpleNamespace(id="code-1", sha256="c" * 64)
        verify.side_effect = [
            (log, Path("/private/rebuild.log")),
            (code, Path("/private/code.zip")),
        ]

        result = update_rebuild(
            self.db, 11, self.user, "REBUILD_FAILED",
            "log-verified", None, "rebuild failed",
        )

        self.assertEqual(result.state, "REBUILD_FAILED")
        self.assertEqual(self.db.add.call_count, 2)
        event = next(
            call.args[0] for call in self.db.add.call_args_list
            if getattr(call.args[0], "event_type", None) == "STEWARD_DIAGNOSIS_REQUESTED"
        )
        self.assertEqual(event.payload["log_artifact_id"], "log-verified")
        self.assertEqual(event.payload["terminal_state"], "REBUILD_FAILED")
        self.db.commit.assert_called_once()

    @patch("logic.mlflow.get_verified_artifact")
    def test_corrupt_source_does_not_prevent_recording_rebuild_failure(self, verify):
        attempt = SimpleNamespace(
            id=11, user_id=7, snapshot_id="snapshot-source", execution_run_id=19,
            state="REBUILD_INITIATED", environment={"python": "3.12"},
            finished_at=None, logs_artifact_id=None, logs_uri=None, failure_summary=None,
        )
        source = SimpleNamespace(
            snapshot_id="snapshot-source", experiment_id=2, project_id="project-1", model_id="model-1", user_id=7,
            code_artifact_id="code-1", code_sha256="c" * 64,
            dependencies={"python": "3.11"}, hyperparameters={"lr": 0.02},
        )
        execution = SimpleNamespace(
            id=19, user_id=7, status="RUNNING", hyperparameters={"lr": 0.02},
            environment={"python": "3.12"}, finished_at=None, logs_artifact_id=None,
            logs_uri=None, error_summary=None, code_artifact_id="code-1",
            code_snapshot_uri="artifact:code-1", code_snapshot_sha256="c" * 64,
            weights_artifact_id=None, weights_uri=None,
        )
        queries = []
        for item in (attempt, source, execution):
            query = MagicMock()
            query.filter.return_value = query
            query.with_for_update.return_value = query
            query.first.return_value = item
            queries.append(query)
        self.db.query.side_effect = queries
        verify.side_effect = BadRequestError("Artifact integrity verification failed")

        result = update_rebuild(
            self.db, 11, self.user, "REBUILD_FAILED", None, None, "source artifact failed integrity check"
        )

        self.assertEqual(result.state, "REBUILD_FAILED")
        self.assertEqual(execution.status, "FAILED")
        self.assertEqual(execution.error_summary, "source artifact failed integrity check")
        self.db.add.assert_not_called()
        self.db.commit.assert_called_once()

    @patch("logic.mlflow.get_verified_artifact")
    def test_corrupt_required_artifact_converts_success_report_to_failed(self, verify):
        attempt = SimpleNamespace(
            id=11, user_id=7, snapshot_id="snapshot-source", execution_run_id=19,
            state="REBUILD_INITIATED", environment={"python": "3.12"},
            finished_at=None, logs_artifact_id=None, logs_uri=None, failure_summary=None,
        )
        source = SimpleNamespace(
            snapshot_id="snapshot-source", experiment_id=2, project_id="project-1",
            model_id="model-1", user_id=7, state="RECORDED", code_artifact_id="code-1",
            weights_artifact_id=None, code_sha256="c" * 64,
            dependencies={"python": "3.11"}, hyperparameters={"lr": 0.02},
        )
        execution = SimpleNamespace(
            id=19, user_id=7, status="RUNNING", hyperparameters={"lr": 0.02},
            environment={"python": "3.12"}, finished_at=None, logs_artifact_id=None,
            logs_uri=None, error_summary=None, code_artifact_id="code-1",
            code_snapshot_uri="artifact:code-1", code_snapshot_sha256="c" * 64,
            weights_artifact_id=None, weights_uri=None,
        )
        queries = []
        for item in (attempt, source, execution):
            query = MagicMock()
            query.filter.return_value = query
            query.with_for_update.return_value = query
            query.first.return_value = item
            queries.append(query)
        self.db.query.side_effect = queries
        verify.side_effect = BadRequestError("Artifact integrity verification failed")

        result = update_rebuild(
            self.db, 11, self.user, "REBUILD_SUCCESS", None, None, None
        )

        self.assertEqual(result.state, "REBUILD_FAILED")
        self.assertEqual(execution.status, "FAILED")
        self.assertIn("integrity verification", result.failure_summary)
        self.db.add.assert_not_called()
        self.db.commit.assert_called_once()

    def test_failed_rebuild_requires_diagnostic_summary(self):
        attempt = SimpleNamespace(id=11, user_id=7, state="REBUILD_INITIATED")
        self.set_owned_item(attempt)

        with self.assertRaises(BadRequestError):
            update_rebuild(self.db, 11, self.user, "REBUILD_FAILED", None, None, None)

        self.db.commit.assert_not_called()

    def test_other_users_rebuild_is_not_found(self):
        query = self.db.query.return_value.filter.return_value
        query.first.return_value = None
        query.with_for_update.return_value.first.return_value = None

        with self.assertRaises(NotFoundError):
            update_rebuild(self.db, 11, self.user, "REBUILD_SUCCESS", None, None, None)

        self.db.commit.assert_not_called()

    @patch("logic.mlflow.get_verified_artifact")
    def test_rebuild_rejects_snapshot_before_creating_attempt_when_artifact_invalid(self, verify):
        snapshot = SimpleNamespace(
            snapshot_id="snapshot-1", experiment_id=2, project_id="project-1",
            model_id="model-1", user_id=7, state="RECORDED",
            code_artifact_id="missing-code", weights_artifact_id=None,
            dependencies={"python": "3.12"},
        )
        self.db.query.return_value.filter.return_value.first.return_value = snapshot
        verify.side_effect = BadRequestError("Artifact integrity verification failed")

        with self.assertRaisesRegex(BadRequestError, "REBUILD_REJECTED"):
            create_rebuild(self.db, "snapshot-1", self.user, {})

        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()


    def test_external_update_cannot_finalize_a_worker_claimed_rebuild(self):
        attempt = SimpleNamespace(
            id=11, user_id=7, state="REBUILD_INITIATED", worker_id="worker-1"
        )
        self.set_owned_item(attempt)

        with self.assertRaisesRegex(ConflictError, "managed by an execution worker"):
            update_rebuild(
                self.db, 11, self.user, "REBUILD_FAILED", None, None, "execution failed"
            )

        self.db.commit.assert_not_called()


    @patch("logic.mlflow.get_verified_artifact")
    def test_rebuild_rejects_snapshot_digest_mismatch_before_creating_attempt(self, verify):
        snapshot = SimpleNamespace(
            snapshot_id="snapshot-1", experiment_id=2, project_id="project-1",
            model_id="model-1", user_id=7, state="RECORDED",
            code_artifact_id="code-1", weights_artifact_id=None,
            code_sha256="a" * 64, dependencies={"python": "3.12"},
        )
        self.db.query.return_value.filter.return_value.first.return_value = snapshot
        verify.return_value = (
            SimpleNamespace(id="code-1", sha256="b" * 64),
            Path("/private/code.zip"),
        )

        with self.assertRaisesRegex(BadRequestError, "REBUILD_REJECTED"):
            create_rebuild(self.db, "snapshot-1", self.user, {})

        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
