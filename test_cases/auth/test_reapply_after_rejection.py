"""A user whose access request was rejected can send a new one; a pending or approved request still blocks it."""
from datetime import datetime, timedelta

from test_cases.helpers.access_api_case import PW, AccessApiCase
from test_cases.helpers.scheduler_pg import requires_postgres


@requires_postgres
class ReapplyAfterRejectionTests(AccessApiCase):
    def try_request(self, auth, label):
        """Send a request without asserting that it succeeds (same roll number as request_access)."""
        return self.client.post("/access-requests", headers=auth, json={
            "name": label, "role": "STUDENT", "course": "BTech", "branch": "CSE", "purpose": "RESEARCH_PROJECT",
            "professor_email": self.prof_email, "roll_number": f"R-{label}-{self.suffix}",
            "start_date": (datetime.utcnow() - timedelta(days=1)).isoformat(),
            "end_date": (datetime.utcnow() + timedelta(days=60)).isoformat()})

    def test_a_rejected_user_can_send_a_new_request_with_the_same_roll_number(self):
        auth = self.new_user("again")
        first = self.request_access(auth, "again")
        self.decide(first, self.prof, "REJECTED")
        r = self.try_request(auth, "again")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["final_status"], "PENDING")
        mine = self.client.get("/access-requests/me", headers=auth).json()
        self.assertEqual([m["final_status"] for m in mine], ["PENDING", "REJECTED"])    # newest first, history kept

    def test_the_new_request_can_be_approved_and_gives_access(self):
        auth = self.new_user("second")
        first = self.request_access(auth, "second")
        self.decide(first, self.admin, "REJECTED")
        second = self.try_request(auth, "second").json()["id"]
        self.decide(second, self.prof)
        self.decide(second, self.admin)
        r = self.client.post("/auth/cli/login", json={
            "email": f"second-{self.suffix}@iitk.ac.in", "password": PW, "role": "STUDENT"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_old_rejected_request_cannot_be_flipped_once_a_newer_one_exists(self):
        auth = self.new_user("flip")
        first = self.request_access(auth, "flip")
        self.decide(first, self.prof, "REJECTED")
        self.assertEqual(self.try_request(auth, "flip").status_code, 201)
        r = self.client.post(f"/access-requests/{first}/decision", headers=self.prof, json={"status": "APPROVED"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("newer request", r.json()["detail"])

    def test_a_rejected_request_can_still_be_changed_while_no_new_request_exists(self):
        auth = self.new_user("mind")
        first = self.request_access(auth, "mind")
        self.decide(first, self.prof, "REJECTED")
        self.decide(first, self.prof, "APPROVED")                # the reviewer changes their mind
        self.assertEqual(self.decide(first, self.admin)["final_status"], "APPROVED")

    def test_a_pending_request_still_blocks_a_second_one(self):
        auth = self.new_user("pend")
        self.request_access(auth, "pend")
        r = self.try_request(auth, "pend")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("PENDING", r.json()["detail"])

    def test_an_approved_user_cannot_send_another_request(self):
        auth, _ = self.approved_user("done")
        r = self.try_request(auth, "done")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("APPROVED", r.json()["detail"])
