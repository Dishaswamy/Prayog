"""A rebuild submitted the way the CLI does it (POST /mlflow/projects/{id}/runs with rebuild_of) is a
RebuildAttempt the Steward can see: a failure is REBUILD_FAILED on the run the Steward seals (S1 and S3,
FR-INT-01), and a first-time run never carries an attempt (FR-STW-002)."""
import tempfile
from pathlib import Path

import models as orm
from database import SessionLocal
from integration.steward_bridge import PlatformRebuildAttempts, run_view
from tests.agent_case import AgentRunCase
from tests.gpu_case import requires_postgres


@requires_postgres
class CliRebuildReachesStewardTests(AgentRunCase):
    def setUp(self):
        super().setUp()
        marker_dir = Path(tempfile.mkdtemp())
        self.marker = marker_dir / "break-the-rebuild"
        self.source = (
            "import pathlib, sys\n"
            f"if pathlib.Path({str(self.marker)!r}).exists():\n"
            "    raise ModuleNotFoundError(\"No module named 'timm.layers.fancy_attention'\")\n"
            "print('trained')\n"
        )

    def finish(self, run) -> dict:
        self.drive(self.make_agent(run), self.finished(run))
        return self.client.get(f"/mlflow/projects/{self.pid}/runs/{run['version']}/{run['run_number']}").json()

    def steward_view(self, job_id: int):
        with SessionLocal() as session:
            run_id = session.query(orm.Run.id).filter(orm.Run.job_id == job_id).scalar()
            return run_view(session, run_id)

    def test_a_failed_cli_rebuild_is_a_failed_attempt_on_the_sealed_run(self):
        original = self.finish(self.submit_program(self.source))
        self.assertEqual(original["status"], "SUCCEEDED", original["error_summary"])
        self.assertIsNone(self.steward_view(original["job_id"]).attempt_id)       # first-time run: no S3

        self.marker.touch()
        body = {"rebuild_of": "A/1", "dataset": [self.dataset_entry("data/train.csv", self.rows)],
                "client_request_id": "cli-rebuild-1"}
        queued = self.submit(self.client, self.pid, **body)
        self.assertEqual(queued.status_code, 201, queued.text)
        queued = queued.json()
        self.assertEqual(self.submit(self.client, self.pid, **body).json()["run_id"], queued["run_id"])
        attempts = self.db.query(orm.RebuildAttempt).filter_by(job_id=queued["job_id"]).all()
        self.assertEqual(len(attempts), 1)                                          # a retry adds none

        done = self.finish(queued)
        self.assertEqual(done["status"], "FAILED")
        view = self.steward_view(queued["job_id"])
        self.assertEqual(view.attempt_id, str(attempts[0].id))
        attempt = PlatformRebuildAttempts(SessionLocal).get(view.attempt_id)
        self.assertEqual((attempt.state, attempt.owner_id, attempt.run_id),
                         ("REBUILD_FAILED", str(self.user.id), view.run_id))
        status = self.client.get(f"/rebuilds/{attempts[0].id}")
        self.assertEqual((status.status_code, status.json()["state"]), (200, "REBUILD_FAILED"))

    def test_a_successful_cli_rebuild_is_a_successful_attempt(self):
        self.finish(self.submit_program(self.source))
        queued = self.submit(self.client, self.pid, rebuild_of="A/1",
                             dataset=[self.dataset_entry("data/train.csv", self.rows)]).json()
        self.assertEqual(self.finish(queued)["status"], "SUCCEEDED")
        attempt_id = self.steward_view(queued["job_id"]).attempt_id
        self.assertEqual(PlatformRebuildAttempts(SessionLocal).get(attempt_id).state, "REBUILD_SUCCESS")
