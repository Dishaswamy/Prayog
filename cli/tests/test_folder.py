import hashlib
import json
import zipfile

import httpx
import pytest

from conftest import log_in_as
from prayog import folder, restore
from prayog.cli import main

PROJECT = {"project_id": "7f3c1e2a-0b1d-4c55-9d3e-123456789abc", "name": "Vision"}


class FakeMlflow:
    """The slice of the ML Flow API that link, update and pull use, kept in memory."""

    def __init__(self):
        self.objects, self.meta = {}, {}
        self.models, self.experiments = [], []
        self.uploads = 0
        self.projects = [PROJECT]
        self.versions = []                       # newest last
        self.current = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if (method, path) == ("GET", "/mlflow/projects"):
            return httpx.Response(200, json=self.projects)
        if (method, path) == ("GET", "/mlflow/experiments"):
            return httpx.Response(200, json=self.experiments)
        if (method, path) == ("POST", "/mlflow/experiments"):
            body = json.loads(request.content)
            self.experiments.append({**body, "id": len(self.experiments) + 1})
            return httpx.Response(201, json=self.experiments[-1])
        if path.endswith("/versions"):
            if method == "POST":
                return self.register(json.loads(request.content)["manifest_artifact_id"])
            return httpx.Response(200, json=[self.version_out(v) for v in reversed(self.versions)])
        if method == "PUT" and path.endswith("/current"):
            wanted = json.loads(request.content)["version"].upper()
            found = next((v for v in self.versions if v["version"] == wanted), None)
            if found is None:
                return httpx.Response(404, json={"detail": f"This project has no version {wanted}."})
            self.current = found["version"]
            return httpx.Response(200, json=self.version_out(found))
        if path.endswith("/models"):
            if method == "POST":
                self.models.append({"model_id": "m" * 36, **json.loads(request.content)})
            return httpx.Response(200, json=self.models) if method == "GET" else httpx.Response(201, json=self.models[-1])
        if path.endswith("/artifacts"):
            if method == "GET":
                return httpx.Response(200, json=list(self.meta.values()))
            self.uploads += 1
            artifact_id = f"artifact-{self.uploads}"
            self.objects[artifact_id] = request.content
            self.meta[artifact_id] = {"id": artifact_id, "kind": request.url.params["kind"],
                                      "sha256": hashlib.sha256(request.content).hexdigest(),
                                      "size_bytes": len(request.content)}
            return httpx.Response(201, json={"id": artifact_id})
        if path.endswith("/content"):
            return httpx.Response(200, content=self.objects[path.split("/")[-2]])
        return httpx.Response(404, json={"detail": "Not found"})


    def version_out(self, version: dict) -> dict:
        return {**version, "is_head": version is self.versions[-1], "is_current": version["version"] == self.current,
                "run_count": 0, "created_at": "2026-10-22T10:00:00", "file_count": 3, "experiment_id": 1}

    def register(self, manifest_id: str) -> httpx.Response:
        digest = hashlib.sha256(self.objects[manifest_id]).hexdigest()
        existing = next((v for v in self.versions if v["manifest_hash"] == digest), None)
        created = existing is None
        if created:
            existing = {"version": chr(ord("A") + len(self.versions)), "version_number": len(self.versions) + 1,
                        "manifest_hash": digest, "manifest_artifact_id": manifest_id}
            self.versions.append(existing)
        self.current = existing["version"]
        return httpx.Response(201, json={**self.version_out(existing), "created": created})


@pytest.fixture
def api():
    return FakeMlflow()


@pytest.fixture
def prayog(api):
    log_in_as()
    return lambda *argv: main(list(argv), transport=httpx.MockTransport(api.handler))


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    (root / "src").mkdir(parents=True)
    (root / "data").mkdir()
    (root / ".git").mkdir()
    (root / ".ssh").mkdir()
    (root / "src" / "main.py").write_text("print('train')\n")
    (root / "requirements.txt").write_text("numpy==2.0.0\n")
    (root / "config.yaml").write_text("lr: 0.01\n")
    (root / "seed.txt").write_text("seed=42\n")
    (root / "data" / "train.csv").write_text("x,y\n1,2\n")
    (root / ".env.example").write_text("URL=http://localhost\n")
    for secret in (".env", "id_rsa", ".git-credentials", "credentials.toml", "secrets.yaml", "terraform.tfstate", "key.pem"):
        (root / secret).write_text("private\n")
    (root / ".ssh" / "config").write_text("private\n")
    (root / ".git" / "config").write_text("private\n")
    return root


def test_link_uploads_the_folder_and_records_the_link(prayog, api, work, capsys):
    assert prayog("project", "link", "vision", str(work)) == 0       # name match ignores case
    out = capsys.readouterr().out
    assert "Uploaded version A of 'Vision'" in out and "1 dataset files fingerprinted" in out
    link = folder.read_link(work)
    assert link["project_id"] == PROJECT["project_id"] and [v["version"] for v in link["versions"]] == ["A"]
    assert api.current == "A"
    assert [e["name"] for e in api.experiments] == ["main"] and [m["name"] for m in api.models] == ["default"]
    manifest = json.loads(api.objects[link["versions"][0]["manifest_artifact_id"]])
    paths = {item["path"]: item for item in manifest["files"]}
    assert set(paths) == {".env.example", "config.yaml", "data/train.csv", "requirements.txt", "seed.txt", "src/main.py"}
    assert paths["data/train.csv"]["role"] == "data" and not paths["data/train.csv"]["included_in_archive"]


def test_link_never_sends_secrets_or_the_data_file(prayog, api, work):
    prayog("project", "link", "Vision", str(work))
    sent = b"".join(api.objects.values())
    assert b"private" not in sent and b"x,y" not in sent


def test_link_needs_an_existing_project(prayog, api, work, capsys):
    api.projects = []
    assert prayog("project", "link", "Vision", str(work)) == 1
    assert "prayog project create Vision" in capsys.readouterr().err
    assert api.uploads == 0 and folder.read_link(work) is None


def test_link_refuses_a_folder_that_is_already_linked(prayog, work, capsys):
    prayog("project", "link", "Vision", str(work))
    assert prayog("project", "link", "Vision", str(work)) == 2
    assert "prayog project update" in capsys.readouterr().err


def test_link_rejects_a_missing_folder(prayog, api, tmp_path, capsys):
    assert prayog("project", "link", "Vision", str(tmp_path / "nope")) == 2
    assert "not a folder" in capsys.readouterr().err and api.uploads == 0


def test_update_with_no_changes_uploads_nothing(prayog, api, work, capsys):
    prayog("project", "link", "Vision", str(work))
    sent = api.uploads
    assert prayog("project", "update", str(work)) == 0
    assert "Nothing changed since version A" in capsys.readouterr().out
    assert api.uploads == sent and len(folder.read_link(work)["versions"]) == 1


def test_update_after_an_edit_records_version_two(prayog, api, work, capsys):
    prayog("project", "link", "Vision", str(work))
    (work / "src" / "main.py").write_text("print('train v2')\n")
    assert prayog("project", "update", str(work)) == 0
    assert "Uploaded version B" in capsys.readouterr().out
    versions = folder.read_link(work)["versions"]
    assert [v["version"] for v in versions] == ["A", "B"] and api.current == "B"
    assert versions[0]["manifest_artifact_id"] != versions[1]["manifest_artifact_id"]


def test_update_needs_a_linked_folder(prayog, work, capsys):
    assert prayog("project", "update", str(work)) == 2
    assert "not linked" in capsys.readouterr().err


def test_a_folder_with_a_symlink_is_refused_and_nothing_is_recorded(prayog, api, work, capsys):
    (work / "alias.py").symlink_to(work / "src" / "main.py")
    assert prayog("project", "link", "Vision", str(work)) == 2
    assert "symbolic links" in capsys.readouterr().err
    assert api.uploads == 0 and folder.read_link(work) is None


def test_pull_restores_the_current_version_and_checks_the_dataset(prayog, api, work, tmp_path, capsys):
    prayog("project", "link", "Vision", str(work))
    dest = tmp_path / "restored"
    assert prayog("project", "pull", "Vision", str(dest), "--data-dir", str(work)) == 0
    assert (dest / "src" / "main.py").read_text() == "print('train')\n"
    assert not (dest / "data").exists() and not (dest / ".env").exists()
    assert "dataset checked, not copied: data/train.csv" in capsys.readouterr().out


def test_pull_needs_no_local_folder(prayog, api, work, tmp_path):
    prayog("project", "link", "Vision", str(work))
    (work / ".prayog" / "link.json").unlink()                            # the folder's own record is gone
    assert prayog("project", "pull", "Vision", str(tmp_path / "again"), "--data-dir", str(work)) == 0
    assert (tmp_path / "again" / "requirements.txt").read_text() == "numpy==2.0.0\n"


def test_pull_picks_the_requested_version_and_defaults_to_current(prayog, api, work, tmp_path):
    prayog("project", "link", "Vision", str(work))
    (work / "src" / "main.py").write_text("print('v2')\n")
    prayog("project", "update", str(work))
    old, new, default = tmp_path / "vA", tmp_path / "vB", tmp_path / "vD"
    assert prayog("project", "pull", "Vision", str(old), "--version", "a", "--data-dir", str(work)) == 0
    assert prayog("project", "pull", "Vision", str(new), "--version", "B", "--data-dir", str(work)) == 0
    assert prayog("project", "pull", "Vision", str(default), "--data-dir", str(work)) == 0       # current is B
    assert (old / "src" / "main.py").read_text() == "print('train')\n"
    assert (new / "src" / "main.py").read_text() == "print('v2')\n"
    assert (default / "src" / "main.py").read_text() == "print('v2')\n"


def test_pull_stops_when_the_dataset_has_changed_or_is_not_given(prayog, api, work, tmp_path, capsys):
    prayog("project", "link", "Vision", str(work))
    assert prayog("project", "pull", "Vision", str(tmp_path / "no-data-dir")) == 2
    assert "--data-dir" in capsys.readouterr().err
    (work / "data" / "train.csv").write_text("x,y\n9,9\n")
    dest = tmp_path / "restored"
    assert prayog("project", "pull", "Vision", str(dest), "--data-dir", str(work)) == 2
    assert "Dataset fingerprint verification failed" in capsys.readouterr().err
    assert not dest.exists()


def test_pull_stops_on_a_tampered_piece(prayog, api, work, tmp_path, capsys):
    prayog("project", "link", "Vision", str(work))
    manifest = json.loads(api.objects[folder.read_link(work)["versions"][0]["manifest_artifact_id"]])
    api.objects[manifest["chunks"][0]["artifact_id"]] = b"tampered"
    dest = tmp_path / "restored"
    assert prayog("project", "pull", "Vision", str(dest), "--data-dir", str(work)) == 2
    assert "size does not match" in capsys.readouterr().err and not dest.exists()


def test_pull_refuses_an_existing_destination(prayog, work, tmp_path, capsys):
    prayog("project", "link", "Vision", str(work))
    (tmp_path / "taken").mkdir()
    assert prayog("project", "pull", "Vision", str(tmp_path / "taken"), "--data-dir", str(work)) == 2
    assert "already exists" in capsys.readouterr().err


def test_pull_with_an_unknown_version_lists_the_known_ones(prayog, work, tmp_path, capsys):
    prayog("project", "link", "Vision", str(work))
    assert prayog("project", "pull", "Vision", str(tmp_path / "x"), "--version", "E") == 2
    err = capsys.readouterr().err
    assert "no version E" in err and "Its versions are: A." in err


def test_pull_before_any_upload_says_how_to_start(prayog, api, tmp_path, capsys):
    assert prayog("project", "pull", "Vision", str(tmp_path / "x")) == 1
    assert "prayog project link Vision FOLDER" in capsys.readouterr().err


def test_relinking_identical_content_reports_the_existing_version(prayog, api, work, tmp_path, capsys):
    prayog("project", "link", "Vision", str(work))
    (work / "src" / "main.py").write_text("print('v2')\n")
    prayog("project", "update", str(work))
    (work / "src" / "main.py").write_text("print('train')\n")                  # the folder is back to version A
    capsys.readouterr()
    assert prayog("project", "update", str(work)) == 0
    assert "matches version A, which was already stored" in capsys.readouterr().out
    assert api.current == "A" and len(api.versions) == 2


def test_extraction_rejects_path_traversal(tmp_path):
    archive_path, staging = tmp_path / "evil.zip", tmp_path / "staging"
    staging.mkdir()
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.txt", "no")
    with pytest.raises(ValueError, match="unsafe file path"):
        restore.extract_archive(archive_path, staging, [])
    assert not (tmp_path / "escape.txt").exists()


def test_logged_out_link_is_just_the_login_hint(run, server, capsys):
    assert run("project", "link", "Vision", ".") == 1
    assert "You are not logged in" in capsys.readouterr().err and server.seen == []
