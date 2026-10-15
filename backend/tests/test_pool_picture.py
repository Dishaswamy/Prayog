"""PRAYOG-80: read-only operational picture of the pool (US-ADM-06; FR-GPU-156, 174; A-33)."""
from datetime import timedelta

import models as orm
from logic.scheduler.domain import AlertKind, JobState, NodeState

from scheduler_pg import SchedulerPostgresCase, requires_postgres


@requires_postgres
class PoolPictureTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", role="ADMIN")
        self.researcher = self.make_user("researcher")
        self.node = self.make_node(units=3)
        self.running = self.make_job(self.researcher, project_ref="classified-model-x",
                                     snapshot_hash="sha256:do-not-show")
        self.make_running(self.running, self.node)
        self.queued = self.make_job(self.researcher, gpus=2, project_ref="classified-model-y")
        free = self.units_of(self.node)[1]
        self.db.add(orm.MaintenanceWindow(unit_id=free.id, starts_at=self.now - timedelta(minutes=5),
                                          ends_at=self.now + timedelta(hours=1), reason="fan swap"))
        self.db.add(orm.NodeAlert(node_id=self.node.id, kind=AlertKind.UNIT_COUNT_MISMATCH,
                                  detail="Registered 3 units; the agent reports 2.", raised_at=self.now))
        self.db.add(orm.UsageEntry(job_id=self.running.id, consumer_lab_id=self.lab.id, provider_lab_id=self.lab.id,
                                   gpu_seconds=7200, ended_at=self.now - timedelta(hours=1)))
        self.db.add(orm.AuditEvent(at=self.now, actor=str(self.admin.id), actor_kind="user",
                                   operation="node.drain", resource_ids=[f"node:{self.node.id}"], outcome="OK",
                                   reason=f"picture-test-{self.suffix}"))
        self.db.commit()

    def _picture(self, user=None, **params):
        response = self.client_as(user or self.admin).get("/admin/pool", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def test_node_and_unit_states_with_the_holding_job_id_only(self):
        body = self._picture().json()
        node = next(n for n in body["nodes"] if n["id"] == self.node.id)
        self.assertEqual(node["state"], NodeState.AVAILABLE)
        self.assertEqual(node["open_alerts"], 1)
        states = [u["state"] for u in node["units"]]
        self.assertEqual(states, ["ALLOCATED", "MAINTENANCE", "FREE"])
        self.assertEqual(node["units"][0]["job_id"], self.running.id)
        self.assertIsNotNone(node["units"][1]["maintenance_until"])

    def test_queue_utilisation_alerts_and_audit(self):
        body = self._picture().json()
        self.assertGreaterEqual(body["queue"]["by_state"][JobState.QUEUED], 1)
        self.assertGreaterEqual(body["queue"]["by_state"][JobState.RUNNING], 1)
        lab = next(q for q in body["queue"]["by_lab"] if q["lab_id"] == self.lab.id)
        self.assertEqual((lab["queued_jobs"], lab["queued_gpus"]), (1, 2))
        self.assertIsNotNone(body["queue"]["oldest_queued_at"])

        utilisation = body["utilisation"]
        self.assertGreaterEqual(utilisation["usable_units"], 2)       # the maintenance unit is not usable
        self.assertGreaterEqual(utilisation["allocated_units"], 1)
        self.assertGreaterEqual(utilisation["gpu_hours_last_24h"], 2.0)

        self.assertTrue(any(a["node_id"] == self.node.id for a in body["alerts"]))
        self.assertTrue(any(e["reason"] == f"picture-test-{self.suffix}" for e in body["audit"]))
        self.assertIsNone(body["hardware_warnings"])                  # the Steward is off in tests

    def test_no_project_contents_ever_appear(self):
        text = self._picture().text
        for secret in ("classified-model-x", "classified-model-y", "sha256:do-not-show",
                       self.running.client_request_id, "project_ref", "snapshot_hash", "client_request_id"):
            self.assertNotIn(secret, text)

    def test_audit_limit_is_bounded_and_the_route_is_admin_only(self):
        self.assertEqual(self._picture(audit_limit=0).json()["audit"], [])
        self.assertEqual(self.client_as(self.admin).get("/admin/pool", params={"audit_limit": 501}).status_code, 422)
        self.assertEqual(self.client_as(self.researcher).get("/admin/pool").status_code, 403)
