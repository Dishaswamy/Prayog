import stat
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
            artifact_id = path.split("/")[-2]
            if artifact_id not in self.objects:
                return httpx.Response(404, json={"detail": "Artifact not found"})
            return httpx.Response(200, content=self.objects[artifact_id])
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

def test_link_rejects_an_empty_folder(prayog, api, tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()

    assert prayog("project", "link", "Vision", str(empty)) == 2
    assert "no files" in capsys.readouterr().err.lower()
    assert api.uploads == 0
    assert folder.read_link(empty) is None


@pytest.mark.parametrize("chunk_size", ["0", "33"])
def test_link_rejects_invalid_chunk_sizes(
    prayog, api, work, capsys, chunk_size
):
    assert prayog(
        "project", "link", "Vision", str(work),
        "--chunk-mib", chunk_size
    ) == 2

    assert "Chunk size must be between 1 and 32 MiB" in capsys.readouterr().err
    assert api.uploads == 0
    assert folder.read_link(work) is None


@pytest.mark.parametrize("chunk_size", ["1", "32"])
def test_link_accepts_chunk_size_boundaries(prayog, api, work, chunk_size):
    assert prayog(
        "project", "link", "Vision", str(work),
        "--chunk-mib", chunk_size
    ) == 0

    assert folder.read_link(work) is not None


def test_inventory_rejects_reserved_manifest_filename(tmp_path):
    root = tmp_path / "work"
    root.mkdir()

    reserved = root / folder.MANIFEST_ENTRY
    reserved.write_text("{}")

    with pytest.raises(ValueError, match="reserved file"):
        folder.inventory(root)

@pytest.mark.parametrize(
    "change",
    [
        {"path": "../escape.txt"},
        {"size_bytes": True},
        {"sha256": "not-a-digest"},
        {"role": "unknown"},
        {"included_in_archive": "yes"},
    ],
)
def test_restore_rejects_invalid_manifest_file_entries(change):
    item = {
        "path": "src/main.py",
        "size_bytes": 10,
        "sha256": "a" * 64,
        "role": "source",
        "included_in_archive": True,
    }
    item.update(change)

    with pytest.raises(ValueError):
        restore.validate_files([item])

def test_pull_handles_a_missing_chunk_without_restoring_partial_files(
    prayog, api, work, tmp_path, capsys
):
    prayog("project", "link", "Vision", str(work))

    manifest_id = folder.read_link(work)["versions"][0]["manifest_artifact_id"]
    manifest = json.loads(api.objects[manifest_id])
    missing_chunk = manifest["chunks"][0]["artifact_id"]

    # Keep its metadata, but simulate the artifact being unavailable.
    del api.objects[missing_chunk]

    dest = tmp_path / "restored"
    assert prayog(
        "project", "pull", "Vision", str(dest), "--data-dir", str(work)
    ) == 1

    assert "Artifact not found" in capsys.readouterr().err
    assert not dest.exists()

def test_pull_rejects_a_reassembled_archive_with_the_wrong_checksum(
    prayog, api, work, tmp_path, capsys
):
    prayog("project", "link", "Vision", str(work))

    manifest_id = folder.read_link(work)["versions"][0]["manifest_artifact_id"]
    manifest = json.loads(api.objects[manifest_id])

    # Make the archive checksum incorrect while keeping the manifest itself valid.
    manifest["archive_sha256"] = "0" * 64
    updated = json.dumps(manifest).encode("utf-8")

    api.objects[manifest_id] = updated
    api.meta[manifest_id]["sha256"] = hashlib.sha256(updated).hexdigest()
    api.meta[manifest_id]["size_bytes"] = len(updated)

    dest = tmp_path / "restored"
    assert prayog(
        "project", "pull", "Vision", str(dest), "--data-dir", str(work)
    ) == 2

    assert "reassembled archive failed its checksum" in capsys.readouterr().err
    assert not dest.exists()
def test_restore_rejects_unhashable_file_role():
    item = {
        "path": "src/main.py",
        "size_bytes": 10,
        "sha256": "a" * 64,
        "role": ["source"],
        "included_in_archive": True,
    }

    with pytest.raises(ValueError, match="role"):
        restore.validate_files([item])

def test_extraction_rejects_symlink_entries(tmp_path):
    archive_path = tmp_path / "symlink.zip"
    staging = tmp_path / "staging"
    staging.mkdir()

    link = zipfile.ZipInfo("evil-link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(link, "outside-target")

    with pytest.raises(ValueError, match="symbolic link or special file"):
        restore.extract_archive(archive_path, staging, [])

    assert not (staging / "evil-link").exists()


def test_extraction_rejects_duplicate_paths(tmp_path):
    archive_path = tmp_path / "duplicate.zip"
    staging = tmp_path / "staging"
    staging.mkdir()

    first_content = b"first"

    expected_files = [
        {
            "path": "src/main.py",
            "size_bytes": len(first_content),
            "sha256": hashlib.sha256(first_content).hexdigest(),
            "included_in_archive": True,
        }
    ]

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("src/main.py", first_content)
        archive.writestr("src/main.py", b"second")

    with pytest.raises(ValueError, match="duplicate paths"):
        restore.extract_archive(archive_path, staging, expected_files)


def test_extraction_rejects_unexpected_files(tmp_path):
    archive_path = tmp_path / "unexpected.zip"
    staging = tmp_path / "staging"
    staging.mkdir()

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("unexpected.txt", "not in manifest")

    with pytest.raises(ValueError, match="absent from its manifest"):
        restore.extract_archive(archive_path, staging, [])

    assert not (staging / "unexpected.txt").exists()


@pytest.mark.parametrize(
    "manifest_content",
    [
        b"{invalid-json",
        json.dumps({"format_version": -1, "files": []}).encode(),
        json.dumps({
            "format_version": restore.FORMAT_VERSION,
            "files": [{"path": "unexpected.txt"}],
        }).encode(),
    ],
)
def test_extraction_rejects_corrupted_internal_manifests(
    tmp_path, manifest_content
):
    archive_path = tmp_path / "corrupted-manifest.zip"
    staging = tmp_path / "staging"
    staging.mkdir()

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(restore.MANIFEST_ENTRY, manifest_content)

    with pytest.raises(ValueError):
        restore.extract_archive(archive_path, staging, [])

def valid_restore_file(**overrides):
    item = {
        "path": "src/main.py",
        "size_bytes": 10,
        "sha256": "a" * 64,
        "role": "source",
        "included_in_archive": True,
    }
    return {**item, **overrides}


def test_restore_digest_and_integer_validators():
    assert restore._is_digest("a" * 64)
    assert not restore._is_digest("g" * 64)
    assert not restore._is_digest("a" * 63)

    assert restore._is_int(10)
    assert not restore._is_int(True)
    assert not restore._is_int(10.0)


@pytest.mark.parametrize(
    "item",
    [
        valid_restore_file(size_bytes=-1),
        valid_restore_file(sha256="g" * 64),
        valid_restore_file(role="unknown"),
        valid_restore_file(included_in_archive=False),
        valid_restore_file(role="data", included_in_archive=True),
    ],
)
def test_restore_rejects_invalid_file_metadata(item):
    with pytest.raises(ValueError):
        restore.validate_files([item])


def test_restore_rejects_duplicate_file_paths():
    item = valid_restore_file()

    with pytest.raises(ValueError, match="duplicate"):
        restore.validate_files([item, item])


def test_restore_rejects_reserved_manifest_path():
    item = valid_restore_file(path=restore.MANIFEST_ENTRY)

    with pytest.raises(ValueError, match="reserved"):
        restore.validate_files([item])


def test_restore_rejects_unsorted_file_inventory():
    first = valid_restore_file(path="z.py")
    second = valid_restore_file(path="a.py")

    with pytest.raises(ValueError, match="not sorted"):
        restore.validate_files([first, second])


def test_restore_rejects_non_list_inventory():
    with pytest.raises(ValueError, match="inventory is invalid"):
        restore.validate_files({"path": "src/main.py"})


def test_restore_rejects_non_dictionary_inventory_entry():
    with pytest.raises(ValueError, match="inventory is invalid"):
        restore.validate_files(["src/main.py"])


def test_restore_rejects_inventory_exceeding_file_limit(monkeypatch):
    monkeypatch.setattr(restore, "MAX_FILES", 1)

    with pytest.raises(ValueError, match="inventory is invalid"):
        restore.validate_files([
            valid_restore_file(path="a.py"),
            valid_restore_file(path="b.py"),
        ])


def test_restore_rejects_inventory_exceeding_total_size(monkeypatch):
    monkeypatch.setattr(restore, "MAX_FOLDER_BYTES", 10)

    with pytest.raises(ValueError, match="4 GiB limit"):
        restore.validate_files([
            valid_restore_file(size_bytes=11),
        ])


def test_extraction_rejects_too_many_entries(tmp_path, monkeypatch):
    archive_path = tmp_path / "too-many.zip"
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(restore, "MAX_FILES", 1)

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("one.txt", "1")
        archive.writestr("two.txt", "2")
        archive.writestr("three.txt", "3")

    with pytest.raises(ValueError, match="too many entries"):
        restore.extract_archive(archive_path, staging, [])


def test_extraction_rejects_directory_with_file_type(tmp_path):
    archive_path = tmp_path / "bad-directory.zip"
    staging = tmp_path / "staging"
    staging.mkdir()

    info = zipfile.ZipInfo("folder/")
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, "")

    with pytest.raises(ValueError, match="directory entry has an invalid type"):
        restore.extract_archive(archive_path, staging, [])


def test_extraction_rejects_file_with_directory_type(tmp_path):
    archive_path = tmp_path / "bad-file.zip"
    staging = tmp_path / "staging"
    staging.mkdir()

    info = zipfile.ZipInfo("not-a-directory")
    info.create_system = 3
    info.external_attr = (stat.S_IFDIR | 0o700) << 16

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, "")

    with pytest.raises(ValueError, match="file entry has an invalid type"):
        restore.extract_archive(archive_path, staging, [])


def test_extraction_handles_directory_entries(tmp_path):
    archive_path = tmp_path / "directory.zip"
    staging = tmp_path / "staging"
    staging.mkdir()

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("src/", "")

    # The directory is created, then extraction fails because the archive
    # does not contain the required internal manifest.
    with pytest.raises(ValueError, match="internal manifest is invalid"):
        restore.extract_archive(archive_path, staging, [])

    assert (staging / "src").is_dir()


def test_extraction_rejects_oversized_internal_manifest(tmp_path, monkeypatch):
    archive_path = tmp_path / "large-manifest.zip"
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(restore, "MAX_MANIFEST_BYTES", 2)

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(restore.MANIFEST_ENTRY, b"123")

    with pytest.raises(ValueError, match="internal manifest is too large"):
        restore.extract_archive(archive_path, staging, [])


def test_extraction_rejects_file_size_mismatch(tmp_path):
    archive_path = tmp_path / "wrong-size.zip"
    staging = tmp_path / "staging"
    staging.mkdir()
    expected = valid_restore_file(size_bytes=10)

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("src/main.py", b"short")

    with pytest.raises(ValueError, match="size does not match"):
        restore.extract_archive(archive_path, staging, [expected])


def test_extraction_rejects_file_checksum_mismatch(tmp_path):
    archive_path = tmp_path / "wrong-checksum.zip"
    staging = tmp_path / "staging"
    staging.mkdir()
    expected = valid_restore_file(
        size_bytes=5,
        sha256="0" * 64,
    )

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("src/main.py", b"hello")

    with pytest.raises(ValueError, match="integrity verification failed"):
        restore.extract_archive(archive_path, staging, [expected])


def test_extraction_rejects_missing_expected_files(tmp_path):
    archive_path = tmp_path / "missing-file.zip"
    staging = tmp_path / "staging"
    staging.mkdir()
    expected = [valid_restore_file()]

    internal_manifest = {
        "format_version": restore.FORMAT_VERSION,
        "files": expected,
    }

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            restore.MANIFEST_ENTRY,
            json.dumps(internal_manifest),
        )

    with pytest.raises(ValueError, match="missing files"):
        restore.extract_archive(archive_path, staging, expected)


def test_check_datasets_returns_when_there_are_no_data_files():
    source_file = valid_restore_file()

    restore._check_datasets([source_file], None)


def test_check_datasets_rejects_nested_symlink(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    outside = tmp_path / "outside"
    outside.write_text("dataset")

    (data_dir / "alias.csv").symlink_to(outside)

    item = valid_restore_file(
        path="alias.csv",
        role="data",
        included_in_archive=False,
    )

    with pytest.raises(ValueError, match="symbolic link"):
        restore._check_datasets([item], data_dir)


def test_check_datasets_rejects_missing_file(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    item = valid_restore_file(
        path="missing.csv",
        role="data",
        included_in_archive=False,
    )

    with pytest.raises(ValueError, match="Dataset file is missing"):
        restore._check_datasets([item], data_dir)

# --- Remaining restore.py validation and failure-path coverage ---


class FakeDownloadResponse:
    def __init__(self, blocks):
        self.blocks = blocks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_bytes(self):
        yield from self.blocks


class FakeDownloadClient:
    def __init__(self, blocks):
        self.response = FakeDownloadResponse(blocks)
        self.requested_path = None

    def stream(self, path):
        self.requested_path = path
        return self.response


def test_download_rejects_piece_exceeding_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(restore, "MAX_ARTIFACT_BYTES", 3)
    client = FakeDownloadClient([b"ab", b"cd"])

    with pytest.raises(ValueError, match="250 MiB limit"):
        restore._download(
            client, "piece-1", tmp_path / "piece.bin", "a" * 64, 4
        )

    assert client.requested_path == "/mlflow/artifacts/piece-1/content"


def test_download_rejects_checksum_mismatch(tmp_path):
    content = b"valid bytes"
    client = FakeDownloadClient([content])

    with pytest.raises(ValueError, match="checksum"):
        restore._download(
            client,
            "piece-2",
            tmp_path / "piece.bin",
            "0" * 64,
            len(content),
        )


def test_extraction_rejects_total_uncompressed_size_limit(
    tmp_path, monkeypatch
):
    archive_path = tmp_path / "oversized-content.zip"
    staging = tmp_path / "staging"
    staging.mkdir()
    content = b"four"
    monkeypatch.setattr(restore, "MAX_FOLDER_BYTES", 3)

    expected = [
        valid_restore_file(
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
    ]

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("src/main.py", content)

    with pytest.raises(ValueError, match="extraction exceeds"):
        restore.extract_archive(archive_path, staging, expected)


def make_valid_chunk_manifest():
    digest = "a" * 64
    chunk = {
        "index": 0,
        "size_bytes": 4,
        "sha256": digest,
        "artifact_id": "piece-1",
    }
    manifest = {
        "chunks": [chunk],
        "chunk_size_bytes": 4,
        "archive_size_bytes": 4,
    }
    artifacts = {
        "piece-1": {
            "kind": "CODE",
            "sha256": digest,
            "size_bytes": 4,
        }
    }
    return manifest, artifacts


@pytest.mark.parametrize(
    "invalid_case",
    [
        "missing_chunks",
        "invalid_chunk_limit",
        "noncanonical_index",
        "invalid_chunk_size",
        "invalid_chunk_digest",
        "short_nonfinal_chunk",
        "invalid_artifact_metadata",
        "archive_size_mismatch",
    ],
)
def test_validated_chunks_rejects_invalid_manifests(invalid_case):
    manifest, artifacts = make_valid_chunk_manifest()

    if invalid_case == "missing_chunks":
        manifest["chunks"] = None
    elif invalid_case == "invalid_chunk_limit":
        manifest["chunk_size_bytes"] = 0
    elif invalid_case == "noncanonical_index":
        manifest["chunks"][0]["index"] = True
    elif invalid_case == "invalid_chunk_size":
        manifest["chunks"][0]["size_bytes"] = 0
    elif invalid_case == "invalid_chunk_digest":
        manifest["chunks"][0]["sha256"] = "not-a-digest"
    
    elif invalid_case == "short_nonfinal_chunk":
        manifest["chunks"][0]["size_bytes"] = 3
        artifacts["piece-1"]["size_bytes"] = 3
        manifest["chunks"].append({
            "index": 1,
            "size_bytes": 1,
            "sha256": "b" * 64,
            "artifact_id": "piece-2",
        })
        artifacts["piece-2"] = {
            "kind": "CODE",
            "sha256": "b" * 64,
            "size_bytes": 1,
        }
        manifest["archive_size_bytes"] = 4
    elif invalid_case == "invalid_artifact_metadata":
        artifacts["piece-1"]["kind"] = "MODEL"
    elif invalid_case == "archive_size_mismatch":
        manifest["archive_size_bytes"] = 5

    with pytest.raises(ValueError):
        restore._validated_chunks(manifest, artifacts)


def test_validated_chunks_rejects_too_many_chunks(monkeypatch):
    monkeypatch.setattr(restore, "MAX_CHUNKS", 1)
    manifest, artifacts = make_valid_chunk_manifest()
    manifest["chunks"].append({
        "index": 1,
        "size_bytes": 1,
        "sha256": "b" * 64,
        "artifact_id": "piece-2",
    })

    with pytest.raises(ValueError, match="chunk manifest is invalid"):
        restore._validated_chunks(manifest, artifacts)


def rewrite_stored_manifest(api, manifest_id, **changes):
    manifest = json.loads(api.objects[manifest_id])
    manifest.update(changes)
    raw = json.dumps(manifest).encode("utf-8")

    api.objects[manifest_id] = raw
    api.meta[manifest_id]["sha256"] = hashlib.sha256(raw).hexdigest()
    api.meta[manifest_id]["size_bytes"] = len(raw)


@pytest.mark.parametrize(
    "metadata_change",
    [
        {"kind": "MODEL"},
        {"size_bytes": restore.MAX_MANIFEST_BYTES + 1},
    ],
)
def test_pull_rejects_invalid_manifest_artifact_metadata(
    prayog, api, work, tmp_path, capsys, metadata_change
):
    prayog("project", "link", "Vision", str(work))
    manifest_id = folder.read_link(work)["versions"][0]["manifest_artifact_id"]
    api.meta[manifest_id].update(metadata_change)

    destination = tmp_path / "restored"
    assert prayog(
        "project", "pull", "Vision", str(destination), "--data-dir", str(work)
    ) == 2

    assert "version was not found" in capsys.readouterr().err
    assert not destination.exists()


def test_pull_rejects_unsupported_manifest_format(
    prayog, api, work, tmp_path, capsys
):
    prayog("project", "link", "Vision", str(work))
    manifest_id = folder.read_link(work)["versions"][0]["manifest_artifact_id"]
    rewrite_stored_manifest(api, manifest_id, format_version=-1)

    destination = tmp_path / "restored"
    assert prayog(
        "project", "pull", "Vision", str(destination), "--data-dir", str(work)
    ) == 2

    assert "manifest format is not supported" in capsys.readouterr().err
    assert not destination.exists()


def test_pull_rejects_manifest_from_another_experiment(
    prayog, api, work, tmp_path, capsys
):
    prayog("project", "link", "Vision", str(work))
    manifest_id = folder.read_link(work)["versions"][0]["manifest_artifact_id"]
    rewrite_stored_manifest(api, manifest_id, experiment_id=999)

    destination = tmp_path / "restored"
    assert prayog(
        "project", "pull", "Vision", str(destination), "--data-dir", str(work)
    ) == 2

    assert "belongs to another project" in capsys.readouterr().err
    assert not destination.exists()


@pytest.mark.parametrize(
    "archive_metadata",
    [
        {"archive_size_bytes": 0},
        {"archive_sha256": "invalid"},
    ],
)
def test_pull_rejects_invalid_archive_metadata(
    prayog, api, work, tmp_path, capsys, archive_metadata
):
    prayog("project", "link", "Vision", str(work))
    manifest_id = folder.read_link(work)["versions"][0]["manifest_artifact_id"]
    rewrite_stored_manifest(api, manifest_id, **archive_metadata)

    destination = tmp_path / "restored"
    assert prayog(
        "project", "pull", "Vision", str(destination), "--data-dir", str(work)
    ) == 2

    assert "archive metadata is invalid" in capsys.readouterr().err
    assert not destination.exists()


def test_pull_cleans_staging_after_extraction_failure(
    prayog, api, work, tmp_path, monkeypatch, capsys
):
    prayog("project", "link", "Vision", str(work))

    def fail_extraction(*args, **kwargs):
        raise ValueError("simulated extraction failure")

    monkeypatch.setattr(restore, "extract_archive", fail_extraction)

    destination = tmp_path / "restored"
    assert prayog(
        "project", "pull", "Vision", str(destination), "--data-dir", str(work)
    ) == 2

    assert "simulated extraction failure" in capsys.readouterr().err
    assert not destination.exists()
    assert list(tmp_path.glob(".restored.*")) == []


# --- folder.py edge cases and failure-path coverage ---


@pytest.mark.parametrize(
    "filename",
    [
        "bad\\name.txt",
        "C:drive-file.txt",
    ],
)
def test_inventory_rejects_invalid_or_drive_qualified_filenames(
    tmp_path, filename
):
    (tmp_path / filename).write_text("content")

    with pytest.raises(ValueError, match="invalid file path|drive-qualified"):
        folder.inventory(tmp_path)


def test_inventory_rejects_symlinked_directory(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private")

    (root / "linked-dir").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic links"):
        folder.inventory(root)


def test_inventory_rejects_excessively_long_relative_path(tmp_path):
    root = tmp_path / "project"
    current = root

    # Five valid path components, but a combined relative path over 1024 chars.
    for index in range(5):
        current = current / (f"segment{index}_" + "x" * 220)
    current.mkdir(parents=True)
    (current / "file.txt").write_text("content")

    with pytest.raises(ValueError, match="Path is too long"):
        folder.inventory(root)


def test_inventory_rejects_total_folder_size_limit(tmp_path, monkeypatch):
    (tmp_path / "large.txt").write_bytes(b"1234")
    monkeypatch.setattr(folder, "MAX_FOLDER_BYTES", 3)

    with pytest.raises(ValueError, match="4 GiB limit"):
        folder.inventory(tmp_path)


def test_inventory_rejects_file_count_limit(tmp_path, monkeypatch):
    (tmp_path / "one.txt").write_text("1")
    (tmp_path / "two.txt").write_text("2")
    monkeypatch.setattr(folder, "MAX_FILES", 1)

    with pytest.raises(ValueError, match="more than 1 files"):
        folder.inventory(tmp_path)


def test_write_archive_rejects_file_changed_during_archiving(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    source = root / "main.py"
    source.write_text("original")

    files = folder.inventory(root)

    # Change the contents after inventory captured the original digest.
    source.write_text("modified")

    with pytest.raises(ValueError, match="changed while it was being uploaded"):
        folder.write_archive(root, tmp_path / "archive.zip", files)


@pytest.mark.parametrize("payload", [b"", b"too-large"])
def test_upload_if_missing_rejects_invalid_payload_size(
    payload, monkeypatch
):
    monkeypatch.setattr(folder, "MAX_ARTIFACT_BYTES", 3)

    with pytest.raises(ValueError, match="upload must be between"):
        folder._upload_if_missing(object(), 1, payload, {})


def test_upload_folder_rejects_oversized_archive(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("print('hello')")

    files = folder.inventory(root)
    monkeypatch.setattr(folder, "MAX_FOLDER_BYTES", 1)

    with pytest.raises(ValueError, match="once archived"):
        folder.upload_folder(object(), 1, root, files)


def test_upload_folder_rejects_folder_changed_during_upload(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()

    source = root / "main.py"
    source.write_text("original")
    files = folder.inventory(root)

    monkeypatch.setattr(folder, "_known_artifacts", lambda *args: {})

    def fake_upload(client, experiment_id, payload, known):
        source.write_text("changed during upload")
        return "artifact-1", True, hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(folder, "_upload_if_missing", fake_upload)

    with pytest.raises(ValueError, match="folder changed while it was being uploaded"):
        folder.upload_folder(object(), 1, root, files, chunk_mib=1)
