"""Shared fixtures for the project-version and GPU-run tests: a real PostgreSQL database (see
scheduler_pg), a private artifact directory per test, and builders for projects and for version
manifests in the exact format `prayog project link` uploads."""
import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from logic.dependencies import get_current_user
from tests.scheduler_pg import SchedulerPostgresCase, requires_postgres  # noqa: F401  (re-exported)


class GpuRunCase(SchedulerPostgresCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.storage = Path(directory.name).resolve()
        for target in ("logic.artifacts.ARTIFACT_ROOT", "logic.artifact_storage.ARTIFACT_ROOT"):
            patcher = patch(target, self.storage)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Scheduling passes that a request would trigger are recorded, never run: a real pass would also place
        # the stale queued jobs other tests leave in the shared database.
        self.passes = []
        for module in ("routers.mlflow_runs", "routers.scheduler.agent", "routers.scheduler.jobs",
                       "routers.scheduler.admin"):
            runner = patch(f"{module}.run_pass", lambda *a, **k: self.passes.append(k))
            runner.start()
            self.addCleanup(runner.stop)

    # ── API as a user ─────────────────────────────────────────────────────

    def client_as(self, user) -> TestClient:
        passes = self.passes
        client = super().client_as(user)
        self.passes = passes                 # the base class starts its own list; keep one shared record
        return client

    def client_for_other(self, user) -> TestClient:
        """A second, independent client signed in as someone else (the override is global, so switch it)."""
        from main import app
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app)

    # ── projects and versions ─────────────────────────────────────────────

    def make_project(self, client: TestClient, name: str | None = None) -> dict:
        project = client.post("/mlflow/projects", json={"name": name or f"proj-{uuid4().hex[:8]}"}).json()
        model = client.post(f"/mlflow/projects/{project['project_id']}/models", json={"name": "default"}).json()
        experiment = client.post("/mlflow/experiments", json={
            "project_id": project["project_id"], "model_id": model["model_id"], "name": "main"}).json()
        return {"project_id": project["project_id"], "experiment_id": experiment["id"]}

    def upload(self, client: TestClient, experiment_id: int, kind: str, payload: bytes) -> dict:
        response = client.post(f"/mlflow/experiments/{experiment_id}/artifacts", params={"kind": kind},
                               content=payload, headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def make_manifest(self, client: TestClient, project: dict, files: dict[str, bytes]) -> str:
        """Upload `files` as one archive chunk plus a manifest, as the CLI does; returns the manifest artifact id."""
        inventory = [{"path": path, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                      "role": "source", "included_in_archive": True} for path, body in sorted(files.items())]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, body in sorted(files.items()):
                archive.writestr(path, body)
            archive.writestr("snapshot-manifest.json", json.dumps({"format_version": 1, "files": inventory}))
        archive_bytes = buffer.getvalue()
        chunk = self.upload(client, project["experiment_id"], "CODE", archive_bytes)
        manifest = {
            "format_version": 1, "experiment_id": project["experiment_id"],
            "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(), "archive_size_bytes": len(archive_bytes),
            "chunk_size_bytes": 8 * 1024 * 1024, "files": inventory,
            "chunks": [{"index": 0, "artifact_id": chunk["id"], "sha256": chunk["sha256"],
                        "size_bytes": chunk["size_bytes"]}],
        }
        body = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        return self.upload(client, project["experiment_id"], "CODE", body)["id"]

    def make_version(self, client: TestClient, project: dict, files: dict[str, bytes] | None = None) -> dict:
        files = files or {"main.py": f"print('{uuid4().hex}')\n".encode()}
        manifest_id = self.make_manifest(client, project, files)
        response = client.post(f"/mlflow/projects/{project['project_id']}/versions",
                               json={"manifest_artifact_id": manifest_id})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # ── datasets ──────────────────────────────────────────────────────────

    @staticmethod
    def blake3_of(payload: bytes) -> str:
        from blake3 import blake3
        return blake3(payload).hexdigest()

    def stage(self, client: TestClient, payload: bytes, size: int | None = None, digest: str | None = None):
        digest = digest or self.blake3_of(payload)
        return client.put(f"/mlflow/staging/files/{digest}", params={"size": len(payload) if size is None else size},
                          content=payload, headers={"Content-Type": "application/octet-stream"})

    def dataset_entry(self, path: str, payload: bytes) -> dict:
        return {"path": path, "size_bytes": len(payload), "blake3": self.blake3_of(payload)}

    def submit(self, client: TestClient, project_id: str, **overrides):
        body = {"gpus": 1, "walltime": "01:00", "client_request_id": uuid4().hex, **overrides}
        return client.post(f"/mlflow/projects/{project_id}/runs", json=body)

    # ── nodes, placement and the agent's API ──────────────────────────────

    def make_agent_node(self, units: int = 2):
        """A registered, enrolled node and its credential, as the agent would hold them."""
        from logic.scheduler import nodes as node_ops
        registration = node_ops.register_node(self.db, f"gpu-{uuid4().hex[:8]}", self.lab.id, "shared", False, units)
        node, credential = node_ops.enrol(self.db, registration.enrolment_token)
        node.state, node.last_heartbeat_at, node.reported_unit_count = "AVAILABLE", self.now, units
        self.db.commit()
        return node, credential

    def place(self, job_id: int, node) -> "object":
        """What a successful scheduling claim leaves: the job SCHEDULED with a live allocation on the node's units."""
        import models as orm
        job = self.db.get(orm.Job, job_id)
        allocation = orm.Allocation(job_id=job.id, node_id=node.id, claimed_at=self.now)
        self.db.add(allocation)
        self.db.flush()
        free = [u for u in self.units_of(node) if u.current_allocation_id is None][: job.gpus]
        for unit in free:
            self.db.add(orm.AllocationUnit(allocation_id=allocation.id, unit_id=unit.id))
            unit.current_allocation_id = allocation.id
        job.state = "SCHEDULED"
        self.db.commit()
        return allocation

    def agent_client(self, credential: str) -> TestClient:
        from main import app
        return TestClient(app, headers={"X-Node-Credential": credential})

    def next_seq(self, node) -> int:
        self.db.refresh(node)
        return node.last_report_seq + 1

    def report(self, agent: TestClient, node, allocation_id: int, outcome="EXITED", exit_code=0, log_tail=None):
        body = {"seq": self.next_seq(node), "outcome": outcome, "exit_code": exit_code, "log_tail": log_tail}
        return agent.post(f"/agent/allocations/{allocation_id}/report", json=body)

    def queued_run(self, client: TestClient, project: dict, version_files=None, **overrides) -> dict:
        """A version, then a run submitted on it; returns the run as the API shows it."""
        self.make_version(client, project, version_files)
        response = self.submit(client, project["project_id"], **overrides)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()
