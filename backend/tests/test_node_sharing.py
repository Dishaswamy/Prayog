"""PRAYOG-79: change a node's donation mode and teaching-pool flag (US-ADM-09; FR-GPU-111, 181)."""
from logic.errors import BadRequestError, ConflictError
from logic.scheduler import nodes as node_ops
from logic.scheduler.domain import JobState, NodeState
from logic.scheduler.eligibility import may_use
from logic.scheduler.loading import load_node_view, to_job_view

from scheduler_pg import SchedulerPostgresCase, requires_postgres


@requires_postgres
class NodeSharingTests(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user("admin", role="ADMIN")
        self.guest_lab = self.make_lab("guest-lab")
        self.guest = self.make_user("guest", lab=self.guest_lab)

    def _guest_may_use(self, node):
        self.db.expire_all()
        job = self.db.get(type(self.guest_job), self.guest_job.id)
        return may_use(to_job_view(job), load_node_view(self.db, self.db.get(type(node), node.id), self.now))

    def test_sharing_applies_to_the_next_pass_and_running_jobs_keep_their_units(self):
        node = self.make_node(donation_mode="reserved")
        owner_job = self.make_job(self.make_user("owner"))
        allocation = self.make_running(owner_job, node)
        self.guest_job = self.make_job(self.guest)
        self.guest_job.lab_id = self.guest_lab.id
        self.db.commit()
        self.assertFalse(self._guest_may_use(node))

        node_ops.change_sharing(self.db, node.id, self.admin.id, "Lab agreed to donate", donation_mode="shared",
                                now=self.now)

        self.assertTrue(self._guest_may_use(node))
        self.db.refresh(allocation)
        self.db.refresh(owner_job)
        self.assertIsNone(allocation.released_at)
        self.assertIsNone(allocation.stop_requested_at)
        self.assertEqual(owner_job.state, JobState.RUNNING)
        [event] = self.audit_rows("node.sharing_change", f"node:{node.id}")
        self.assertEqual(event.actor, str(self.admin.id))
        self.assertIn("donation_mode:reserved->shared", event.resource_ids)
        self.assertEqual(event.reason, "Lab agreed to donate")

    def test_reserved_teaching_node_is_refused_either_way_round(self):
        reserved = self.make_node("reserved", donation_mode="reserved")
        teaching = self.make_node("teaching", donation_mode="shared", teaching_pool=True)

        with self.assertRaises(BadRequestError):
            node_ops.change_sharing(self.db, reserved.id, self.admin.id, "for CS455", teaching_pool=True)
        with self.assertRaises(BadRequestError):
            node_ops.change_sharing(self.db, teaching.id, self.admin.id, "lab wants it back", donation_mode="reserved")

        self.db.expire_all()
        self.assertEqual((reserved.donation_mode, reserved.teaching_pool), ("reserved", False))
        self.assertEqual((teaching.donation_mode, teaching.teaching_pool), ("shared", True))
        self.assertEqual(self.audit_rows("node.sharing_change", f"node:{reserved.id}"), [])

    def test_both_fields_can_change_together(self):
        node = self.make_node(donation_mode="shared", teaching_pool=True)
        node_ops.change_sharing(self.db, node.id, self.admin.id, "Term over", donation_mode="reserved",
                                teaching_pool=False)
        self.db.refresh(node)
        self.assertEqual((node.donation_mode, node.teaching_pool), ("reserved", False))

    def test_reason_and_a_change_are_required_and_retired_nodes_are_refused(self):
        node = self.make_node()
        with self.assertRaises(BadRequestError):
            node_ops.change_sharing(self.db, node.id, self.admin.id, " ", donation_mode="reserved")
        with self.assertRaises(BadRequestError):
            node_ops.change_sharing(self.db, node.id, self.admin.id, "nothing")
        retired = self.make_node("retired", state=NodeState.DECOMMISSIONED)
        with self.assertRaises(ConflictError):
            node_ops.change_sharing(self.db, retired.id, self.admin.id, "too late", donation_mode="reserved")

    def test_route_is_admin_only_and_triggers_a_pass(self):
        node = self.make_node(donation_mode="reserved")
        body = {"donation_mode": "shared", "teaching_pool": True, "reason": "Donated to the course"}

        self.assertEqual(self.client_as(self.guest).patch(f"/admin/nodes/{node.id}/sharing", json=body).status_code, 403)
        bad = self.client_as(self.admin).patch(f"/admin/nodes/{node.id}/sharing",
                                               json={"teaching_pool": True, "reason": "x"})
        self.assertEqual(bad.status_code, 400)

        ok = self.client_as(self.admin).patch(f"/admin/nodes/{node.id}/sharing", json=body)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual((ok.json()["donation_mode"], ok.json()["teaching_pool"]), ("shared", True))
        self.assertEqual(len(self.passes), 1)
