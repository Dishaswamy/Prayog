"""Shared setup for tests that need real approved/unapproved users through the public API."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from database import SessionLocal
from logic.auth import create_user_record
from main import app
from tests.scheduler_pg import SchedulerPostgresCase

PW = "AccessPass12345"


class AccessApiCase(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self.admin_email = f"admin-{self.suffix}@iitk.ac.in"
        with SessionLocal() as db:
            create_user_record(db, self.admin_email, PW, "ADMIN")
            db.commit()
        self.admin = self.login(self.admin_email, PW, "ADMIN")
        self.prof_email = f"prof-{self.suffix}@iitk.ac.in"
        r = self.client.post("/professors", headers=self.admin, json={
            "name": "Prof Access", "email": self.prof_email, "department": "CSE", "lab_id": self.lab.id})
        assert r.status_code == 201, r.text
        self.prof_password = r.json()["temporary_password"]
        self.prof = self.login(self.prof_email, self.prof_password, "PROFESSOR")

    def login(self, email, password, role):
        r = self.client.post("/auth/login", json={"email": email, "password": password, "role": role})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def new_user(self, label, role="STUDENT"):
        email = f"{label}-{self.suffix}@iitk.ac.in"
        r = self.client.post("/auth/register", json={"email": email, "password": PW, "role": role})
        assert r.status_code == 201, r.text
        return self.login(email, PW, role)

    def request_access(self, auth, label, role="STUDENT", end_in_days=60):
        body = {"name": label, "role": role, "course": "BTech", "branch": "CSE", "purpose": "RESEARCH_PROJECT",
                "professor_email": self.prof_email, "roll_number": f"R-{label}-{self.suffix}",
                "start_date": (datetime.utcnow() - timedelta(days=1)).isoformat()}
        if role == "STUDENT":
            body["end_date"] = (datetime.utcnow() + timedelta(days=end_in_days)).isoformat()
        r = self.client.post("/access-requests", headers=auth, json=body)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def decide(self, request_id, who, status="APPROVED"):
        r = self.client.post(f"/access-requests/{request_id}/decision", headers=who, json={"status": status})
        assert r.status_code == 200, r.text
        return r.json()

    def approved_user(self, label, role="STUDENT", **kw):
        auth = self.new_user(label, role)
        request_id = self.request_access(auth, label, role, **kw)
        self.decide(request_id, self.prof)
        self.decide(request_id, self.admin)
        return auth, request_id
