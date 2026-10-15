"""PRAYOG-82: investigate a suspected hardware fault (US-ADM-05; FR-GPU-158, 163; A-29, A-36)."""
import os
import uuid
from datetime import timedelta
from unittest.mock import patch

import models as orm
from integration import hardware_investigation as hardware
from logic.errors import BadRequestError, ConflictError, NotFoundError
from logic.scheduler.domain import JobState, NodeState
from routers.scheduler.admin import get_hardware_warnings

from scheduler_pg import SchedulerPostgresCase, requires_postgres


class FakeWarnings:
    """Stands in for the Steward's warning store."""

    def __init__(self):
        self.warnings = {}
        self.closed = []

    def add(self, unit_signature: str, state: str = "OPEN") -> uuid.UUID:
        warning_id = uuid.uuid4()
        self.warnings[warning_id] = {
            "warning_id": str(warning_id), "unit_signature": unit_signature, "state": state,
            "evidence": [{"run_id": "41", "job_id": "17", "owner_lab": "NLP", "exit_code": 139,
                          "failure_category": "CUDA_ERROR", "hardware_verdict": "LIKELY_HARDWARE"}],
            "allocation_history": [{"unit_id": unit_signature, "job_id": "17", "lab": "NLP"}],
            "steward_finding": {"verdict": "LIKELY_HARDWARE", "indicators": ["Xid 79"]},
        }
        return warning_id

    def detail(self, warning_id):
        return self.warnings.get(warning_id)

    def close(self, warning_id, admin_id, resolution, reason, now):
        if self.warnings[warning_id]["state"] != "OPEN":
            raise ConflictError("This warning is already closed.")
        self.warnings[warning_id]["state"] = resolution
        self.closed.append((warning_id, admin_id, resolution, reason))


@requires_postgres
class HardwareInvestigationTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", role="ADMIN")
        self.researcher = self.make_user("researcher")
        self.node = self.make_node(units=2)
        self.unit = self.units_of(self.node)[1]
        self.signature = f"{self.node.name}/gpu1"
        self.warnings = FakeWarnings()
        self.warning_id = self.warnings.add(self.signature)

    def _resolve(self, action, reason="Xid 79 on three labs' runs", **kw):
        return hardware.resolve(self.db, self.warnings, self.warning_id, self.admin.id, action, reason,
                                now=self.now, **kw)

    def test_investigation_shows_cited_runs_history_finding_and_the_unit_state(self):
        view = hardware.investigate(self.db, self.warnings, self.warning_id, now=self.now)
        self.assertEqual(view["warning"]["evidence"][0]["run_id"], "41")
        self.assertEqual(view["warning"]["steward_finding"]["verdict"], "LIKELY_HARDWARE")
        self.assertEqual(view["unit"]["unit_id"], self.unit.id)
        self.assertEqual(view["unit"]["node_state"], NodeState.AVAILABLE)
        self.assertFalse(view["unit"]["allocated"])
        with self.assertRaises(NotFoundError):
            hardware.investigate(self.db, self.warnings, uuid.uuid4())

    def test_freeze_books_a_window_on_the_unit_from_now_and_resolves_the_warning(self):
        until = self.now + timedelta(days=2)
        decision = self._resolve("freeze", freeze_until=until)

        self.assertEqual((decision.resolution, decision.unit_id), ("RESOLVED", self.unit.id))
        [window] = self.db.query(orm.MaintenanceWindow).filter_by(booking_id=decision.booking_id).all()
        self.assertEqual((window.unit_id, window.starts_at, window.ends_at), (self.unit.id, self.now, until))
        self.assertEqual(self.warnings.closed, [(self.warning_id, str(self.admin.id), "RESOLVED",
                                                 "Xid 79 on three labs' runs")])
        [event] = self.audit_rows("hardware_warning.freeze", f"warning:{self.warning_id}")
        self.assertEqual((event.actor, event.outcome), (str(self.admin.id), "RESOLVED"))
        view = hardware.investigate(self.db, self.warnings, self.warning_id, now=self.now)
        self.assertEqual(view["unit"]["maintenance_until"], until)

    def test_freeze_that_would_stop_a_running_job_needs_confirmation(self):
        job = self.make_job(self.researcher, gpus=2)
        self.make_running(job, self.node)
        with self.assertRaises(hardware.maintenance.CollisionNotConfirmed):
            self._resolve("freeze", freeze_until=self.now + timedelta(hours=1))
        self.assertEqual(self.warnings.warnings[self.warning_id]["state"], "OPEN")

        decision = self._resolve("freeze", freeze_until=self.now + timedelta(hours=1), confirm=True)
        self.assertEqual(decision.resolution, "RESOLVED")

    def test_drain_takes_the_node_out_of_service_with_the_reason(self):
        decision = self._resolve("drain", drain_mode="hard")
        self.db.refresh(self.node)
        self.assertEqual((self.node.state, self.node.drain_mode), (NodeState.DRAINING, "hard"))
        self.assertIn("Xid 79", self.node.state_reason)
        self.assertEqual(decision.resolution, "RESOLVED")

    def test_dismiss_changes_nothing_but_closes_the_warning(self):
        decision = self._resolve("dismiss", reason="User code bug, not hardware")
        self.db.refresh(self.node)
        self.assertEqual(self.node.state, NodeState.AVAILABLE)
        self.assertEqual(self.db.query(orm.MaintenanceWindow).filter_by(unit_id=self.unit.id).count(), 0)
        self.assertEqual(decision.resolution, "DISMISSED")
        self.assertEqual(self.warnings.warnings[self.warning_id]["state"], "DISMISSED")

    def test_reason_is_required_and_a_closed_warning_cannot_be_resolved_again(self):
        with self.assertRaises(BadRequestError):
            self._resolve("dismiss", reason="  ")
        with self.assertRaises(BadRequestError):
            self._resolve("freeze")                                  # no freeze_until
        self._resolve("dismiss")
        with self.assertRaises(ConflictError):
            self._resolve("drain")
        self.db.refresh(self.node)
        self.assertEqual(self.node.state, NodeState.AVAILABLE)

    def test_a_vanished_unit_can_only_be_dismissed(self):
        gone = self.warnings.add("no-such-node/gpu0")
        with self.assertRaises(ConflictError):
            hardware.resolve(self.db, self.warnings, gone, self.admin.id, "drain", "faulty")
        decision = hardware.resolve(self.db, self.warnings, gone, self.admin.id, "dismiss", "node replaced")
        self.assertEqual((decision.resolution, decision.unit_id), ("DISMISSED", None))

    def test_without_the_steward_the_routes_answer_503(self):
        with patch.dict(os.environ, {"STEWARD_ENABLED": "false"}):
            response = self.client_as(self.admin).get(f"/admin/hardware-warnings/{self.warning_id}")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "STEWARD_UNAVAILABLE")

    def test_routes_are_admin_only_and_report_collisions(self):
        from main import app

        app.dependency_overrides[get_hardware_warnings] = lambda: self.warnings
        self.addCleanup(app.dependency_overrides.pop, get_hardware_warnings, None)
        self.assertEqual(self.client_as(self.researcher).get(f"/admin/hardware-warnings/{self.warning_id}")
                         .status_code, 403)

        client = self.client_as(self.admin)
        view = client.get(f"/admin/hardware-warnings/{self.warning_id}")
        self.assertEqual(view.status_code, 200, view.text)
        self.assertEqual(view.json()["unit"]["unit_index"], 1)

        job = self.make_job(self.researcher, gpus=2)
        self.make_running(job, self.node)
        until = (self.now + timedelta(hours=3)).isoformat()
        collision = client.post(f"/admin/hardware-warnings/{self.warning_id}/resolve",
                                json={"action": "freeze", "reason": "Xid 79", "freeze_until": until})
        self.assertEqual(collision.status_code, 409)
        self.assertEqual(collision.json()["collisions"][0]["job_id"], job.id)

        done = client.post(f"/admin/hardware-warnings/{self.warning_id}/resolve",
                           json={"action": "drain", "reason": "Xid 79", "drain_mode": "soft"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["resolution"], "RESOLVED")
        self.assertEqual(len(self.passes), 1)
        self.db.refresh(job)
        self.assertEqual(job.state, JobState.RUNNING)                 # a soft drain lets it finish
