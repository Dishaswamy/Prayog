import json

import httpx
import pytest

from conftest import log_in_as
from prayog import dataset
from prayog.cli import main
from prayog.commands.run import parse_hyperparameters
from prayog.errors import CliError

PID = "7f3c1e2a-0b1d-4c55-9d3e-123456789abc"
PROJECT = {"project_id": PID, "name": "Vision"}
BASE = f"/mlflow/projects/{PID}"


def version(letter, number, current=False, head=False, runs=0):
    return {"version": letter, "version_number": number, "manifest_hash": letter.lower() * 64,
            "manifest_artifact_id": "m" * 36, "experiment_id": 1, "file_count": 12, "created_at": "2026-10-20T09:00:00",
            "is_head": head, "is_current": current, "run_count": runs}


def run_json(label="A run 1", status="RUNNING", job_state="RUNNING", **over):
    base = {"run_id": 1, "label": label, "version": label[0], "run_number": int(label.split()[-1]), "job_id": 41,
            "status": status, "job_state": job_state, "queue_reason": None, "gpus": 2, "walltime": "04:00",
            "hyperparameters": {}, "dataset": [], "checkpoints": [], "metrics": {}, "logs_artifact_id": None,
            "snapshot_id": None, "error_summary": None, "created_at": "2026-10-22T10:00:00",
            "started_at": None, "finished_at": None, "created": True}
    return {**base, **over}


@pytest.fixture(autouse=True)
def signed_in(server):
    log_in_as()
    server.on("GET", "/mlflow/projects", 200, [PROJECT])


# ── versions, current, checkout ───────────────────────────────────────

def test_versions_are_listed_newest_first_with_head_and_current_marked(run, server, capsys):
    server.on("GET", f"{BASE}/versions", 200, [version("B", 2, head=True, runs=3), version("A", 1, current=True, runs=1)])
    assert run("project", "versions", "vision") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["VERSION", "FILES", "RUNS", "UPLOADED", "ID"]
    assert lines[2].split() == ["B", "head", "12", "3", "2026-10-20", "bbbbbbbbbbbb"]
    assert lines[3].split() == ["A", "current", "12", "1", "2026-10-20", "aaaaaaaaaaaa"]


def test_versions_of_an_empty_project_say_how_to_start(run, server, capsys):
    server.on("GET", f"{BASE}/versions", 200, [])
    assert run("project", "versions", "Vision") == 1
    assert "prayog project link Vision FOLDER" in capsys.readouterr().err


def test_an_unknown_project_name_points_at_create(run, server, capsys):
    assert run("project", "versions", "Nope") == 1
    assert "prayog project create Nope" in capsys.readouterr().err


def test_current_says_whether_it_is_the_newest(run, server, capsys):
    server.on("GET", f"{BASE}/versions", 200, [version("B", 2, head=True), version("A", 1, current=True)])
    run("project", "current", "Vision")
    assert "Current version of 'Vision': A (older than the newest, B)." in capsys.readouterr().out
    server.on("GET", f"{BASE}/versions", 200, [version("B", 2, head=True, current=True), version("A", 1)])
    run("project", "current", "Vision")
    assert "B (the newest version)" in capsys.readouterr().out


def test_checkout_sends_the_version_and_reports_the_result(run, server, capsys):
    server.on("PUT", f"{BASE}/current", 200, version("A", 1, current=True))
    assert run("project", "checkout", "Vision", "a") == 0
    assert server.body() == {"version": "a"}
    assert "now uses version A (not the newest version)" in capsys.readouterr().out


def test_checkout_of_a_missing_version_shows_the_servers_reason(run, server, capsys):
    server.on("PUT", f"{BASE}/current", 404, {"detail": "This project has no version Q."})
    assert run("project", "checkout", "Vision", "Q") == 1
    assert "no version Q" in capsys.readouterr().err


# ── status and runs ───────────────────────────────────────────────────

def summary(**over):
    base = {"project_id": PID, "name": "Vision", "current_version": "B", "head_version": "B", "versions": 2, "runs": 1,
            "latest_run": None, "staged_files": 0, "staged_bytes": 0}
    return {**base, **over}


def test_status_shows_the_latest_run_in_full(run, server, capsys):
    latest = run_json(
        status="SUCCEEDED", job_state="COMPLETED", started_at="2026-10-22T10:05:00", finished_at="2026-10-22T11:05:00",
        metrics={"loss": 0.25, "acc": 0.91},
        checkpoints=[{"step": 1, "is_best": False, "artifact_id": "a", "size_bytes": 5},
                     {"step": 5, "is_best": True, "artifact_id": "b", "size_bytes": 5}],
        dataset=[{"path": "d.csv", "size_bytes": 2048, "blake3": "0" * 64, "staged": True}], snapshot_id="snap-1")
    server.on("GET", f"{BASE}/status", 200, summary(latest_run=latest, staged_files=1, staged_bytes=2048))
    assert run("project", "status", "Vision") == 0
    out = capsys.readouterr().out
    assert "current version B" in out and "1 run(s)" in out
    assert "A run 1: SUCCEEDED (COMPLETED)" in out
    assert "2 GPU(s), walltime 04:00" in out
    assert "started 2026-10-22 10:05:00, ended 2026-10-22 11:05:00" in out
    assert "metrics (last value): acc=0.91, loss=0.25" in out
    assert "checkpoints: 2 saved (steps 1 to 5), best at step 5" in out
    assert "dataset: 1 files (2.0 KB), 1 still staged" in out
    assert "sealed as snapshot snap-1" in out
    assert "Temporary dataset copies held: 1 files (2.0 KB)" in out


def test_status_of_a_waiting_run_says_why(run, server, capsys):
    waiting = run_json(status="QUEUED", job_state="QUEUED", queue_reason="WAITING_FOR_UNITS")
    server.on("GET", f"{BASE}/status", 200, summary(latest_run=waiting))
    run("project", "status", "Vision")
    assert "A run 1: QUEUED, waiting: WAITING_FOR_UNITS" in capsys.readouterr().out


def test_status_of_a_failed_run_shows_the_reason_and_the_deleted_data(run, server, capsys):
    failed = run_json(status="FAILED", job_state="FAILED", error_summary="The program exited with code 3.",
                      dataset=[{"path": "d.csv", "size_bytes": 10, "blake3": "0" * 64, "staged": False}])
    server.on("GET", f"{BASE}/status", 200, summary(latest_run=failed))
    run("project", "status", "Vision")
    out = capsys.readouterr().out
    assert "The program exited with code 3." in out
    assert "temporary copy deleted; fingerprint kept" in out


def test_status_notes_when_current_is_not_the_newest(run, server, capsys):
    server.on("GET", f"{BASE}/status", 200, summary(current_version="A", head_version="B"))
    run("project", "status", "Vision")
    out = capsys.readouterr().out
    assert "current version A (newest is B)" in out and "No runs yet." in out


def test_status_can_name_a_run(run, server, capsys):
    server.on("GET", f"{BASE}/status", 200, summary())
    server.on("GET", f"{BASE}/runs/A/2", 200, run_json("A run 2", status="RUNNING", job_state="RUNNING"))
    assert run("project", "status", "Vision", "--run", "A/2") == 0
    assert "A run 2: RUNNING" in capsys.readouterr().out
    assert run("project", "status", "Vision", "--run", "A2") == 2


def test_status_before_any_upload_says_how_to_start(run, server, capsys):
    server.on("GET", f"{BASE}/status", 200, summary(current_version=None, head_version=None, versions=0, runs=0))
    assert run("project", "status", "Vision") == 1
    assert "prayog project link" in capsys.readouterr().err


def test_runs_are_listed_as_a_table(run, server, capsys):
    server.on("GET", f"{BASE}/runs", 200, [run_json("B run 1", checkpoints=[{"step": 1, "is_best": False, "artifact_id": "a", "size_bytes": 1}]),
                                        run_json("A run 2", status="FAILED", job_state="FAILED")])
    assert run("project", "runs", "Vision") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[2].split()[:6] == ["B", "run", "1", "RUNNING", "2", "04:00"]
    assert lines[3].split()[:6] == ["A", "run", "2", "FAILED", "2", "04:00"]


def test_runs_can_be_limited_to_a_version(run, server):
    server.on("GET", f"{BASE}/runs", 200, [])
    run("project", "runs", "Vision", "--version", "A")
    assert server.seen[-1].url.params["version"] == "A"


def test_no_runs_yet_suggests_the_run_command(run, server, capsys):
    server.on("GET", f"{BASE}/runs", 200, [])
    assert run("project", "runs", "Vision") == 0
    assert "prayog run Vision --gpus 1 --walltime 01:00" in capsys.readouterr().out


def test_clean_reports_what_was_deleted_and_kept(run, server, capsys):
    server.on("DELETE", f"{BASE}/staged-data", 200, {"deleted_files": 2, "kept_in_use": 1, "freed_bytes": 3 * 1024 * 1024})
    assert run("project", "clean", "Vision") == 0
    out = capsys.readouterr().out
    assert "Deleted 2 staged file(s), freeing 3.0 MB." in out and "Kept 1 file(s) that a job still needs." in out


# ── prayog run ────────────────────────────────────────────────────────

QUEUED = run_json("A run 3", status="QUEUED", job_state="IN_PROCESS")


def test_run_submits_with_gpus_walltime_and_hyperparameters(run, server, capsys):
    server.on("POST", f"{BASE}/runs", 201, QUEUED)
    assert run("run", "Vision", "--gpus", "2", "--walltime", "04:00", "--hp", "lr=0.01", "--hp", "name=resnet",
               "--hp", "layers=[1,2]") == 0
    body = server.body()
    assert (body["gpus"], body["walltime"], body["entrypoint"], body["dataset"]) == (2, "04:00", "main.py", [])
    assert body["hyperparameters"] == {"lr": 0.01, "name": "resnet", "layers": [1, 2]}
    assert "version" not in body and len(body["client_request_id"]) == 32
    out = capsys.readouterr().out
    assert "Queued A run 3 (job 41): 2 GPU(s), walltime 04:00." in out
    assert "prayog project status Vision" in out


def test_run_on_a_chosen_version_and_entrypoint(run, server):
    server.on("POST", f"{BASE}/runs", 201, QUEUED)
    run("run", "Vision", "--walltime", "01:00", "--version", "A", "--entrypoint", "train.py")
    assert (server.body()["version"], server.body()["entrypoint"], server.body()["gpus"]) == ("A", "train.py", 1)


def test_run_needs_a_walltime(run, server):
    with pytest.raises(SystemExit):
        run("run", "Vision")
    assert server.seen == []


@pytest.mark.parametrize("args,fragment", [(["--gpus", "0"], "at least 1"), (["--hp", "oops"], "KEY=VALUE"), (["--hp", "=3"], "KEY=VALUE")])
def test_run_rejects_bad_options_before_contacting_the_server(run, server, capsys, args, fragment):
    assert run("run", "Vision", "--walltime", "01:00", *args) == 2
    assert fragment in capsys.readouterr().err and server.seen == []


def test_the_servers_rejection_of_a_walltime_is_shown(run, server, capsys):
    server.on("POST", f"{BASE}/runs", 400, {"detail": "Walltime must be HH:MM and at least 00:05, got 'soon'.", "code": "INVALID_WALLTIME"})
    assert run("run", "Vision", "--walltime", "soon") == 1
    assert "Walltime must be HH:MM" in capsys.readouterr().err


def test_run_without_an_uploaded_version_shows_how_to_start(run, server, capsys):
    server.on("POST", f"{BASE}/runs", 404, {"detail": "This project has no uploaded version yet. Upload a folder with `prayog project link`."})
    assert run("run", "Vision", "--walltime", "01:00") == 1
    assert "prayog project link" in capsys.readouterr().err


def test_run_is_for_users_who_do_work_only(run, server, capsys):
    log_in_as("ADMIN", "admin@iitk.ac.in")
    assert run("run", "Vision", "--walltime", "01:00") == 1
    assert "not available for your role (ADMIN)" in capsys.readouterr().err and server.seen == []


def test_logged_out_run_is_the_login_hint(run, server, capsys):
    from prayog import credentials
    credentials.clear()
    assert run("run", "Vision", "--walltime", "01:00") == 1
    assert capsys.readouterr().err == "Error: You are not logged in. Run `prayog login`.\n"


class FlakyNetwork(httpx.BaseTransport):
    """Fails the first `failures` submissions at the network, then answers."""

    def __init__(self, inner, failures):
        self.inner, self.failures, self.ids = inner, failures, []

    def handle_request(self, request):
        if request.method == "POST" and request.url.path.endswith("/runs"):
            self.ids.append(json.loads(request.content)["client_request_id"])
            if self.failures:
                self.failures -= 1
                raise httpx.ConnectError("down", request=request)
        return self.inner.handle_request(request)


def test_a_dropped_connection_is_retried_with_the_same_request_id(server, monkeypatch, capsys):
    monkeypatch.setattr("prayog.commands.run.time.sleep", lambda s: None)
    server.on("POST", f"{BASE}/runs", 201, QUEUED)
    network = FlakyNetwork(server.transport, failures=2)
    assert main(["run", "Vision", "--walltime", "01:00"], transport=network) == 0
    assert len(network.ids) == 3 and len(set(network.ids)) == 1               # one request ID: no double submission


def test_giving_up_after_repeated_network_failures(server, monkeypatch, capsys):
    monkeypatch.setattr("prayog.commands.run.time.sleep", lambda s: None)
    network = FlakyNetwork(server.transport, failures=99)
    assert main(["run", "Vision", "--walltime", "01:00"], transport=network) == 1
    assert len(network.ids) == 3
    assert "Could not reach the Prayog server" in capsys.readouterr().err


# ── datasets on the command line ──────────────────────────────────────

@pytest.fixture
def data(tmp_path):
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    (root / "a.csv").write_bytes(b"x,y\n1,2\n")
    (root / "sub" / "b.csv").write_bytes(b"3,4\n")
    (root / ".env").write_text("SECRET=1")
    (root / "key.pem").write_text("private")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("private")
    return root


class StagingServer:
    """The staging endpoints, kept in memory, on top of the plain fake server."""

    def __init__(self, server, held=()):
        self.server, self.held, self.uploaded = server, set(held), {}
        server.on("POST", f"{BASE}/runs", 201, QUEUED)
        self.inner = server.handler
        server.handler = self.handler

    def handler(self, request):
        if request.url.path == "/mlflow/staging/missing":
            files = json.loads(request.content)["files"]
            self.server.seen.append(request)
            return httpx.Response(200, json={"missing": sorted({f["blake3"] for f in files} - self.held)})
        if request.url.path.startswith("/mlflow/staging/files/"):
            self.server.seen.append(request)
            digest = request.url.path.rsplit("/", 1)[-1]
            self.uploaded[digest] = (request.content, int(request.url.params["size"]), request.method)
            self.held.add(digest)
            return httpx.Response(201, json={"blake3": digest, "size_bytes": len(request.content), "created": True})
        return self.inner(request)


def test_the_dataset_is_fingerprinted_with_blake3_and_secrets_are_left_out(data):
    found = {f.path: f for f in dataset.scan(data)}
    assert sorted(found) == ["a.csv", "sub/b.csv"]
    from blake3 import blake3
    assert found["a.csv"].blake3 == blake3(b"x,y\n1,2\n").hexdigest() and found["a.csv"].size_bytes == 8


def test_a_single_file_is_staged_under_its_own_name(data):
    assert [f.path for f in dataset.scan(data / "a.csv")] == ["a.csv"]


def test_a_symlink_in_the_dataset_is_refused(data):
    (data / "alias.csv").symlink_to(data / "a.csv")
    with pytest.raises(CliError, match="symbolic link"):
        dataset.scan(data)


def test_an_empty_or_missing_dataset_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(CliError, match="no files"):
        dataset.scan(tmp_path / "empty")
    with pytest.raises(CliError, match="not a file or folder"):
        dataset.scan(tmp_path / "nope")


def test_two_datasets_may_not_claim_the_same_path(tmp_path):
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.csv").write_text(name)
    with pytest.raises(CliError, match="both be 'x.csv'"):
        dataset.combine([tmp_path / "one", tmp_path / "two"])


def test_run_uploads_only_the_files_the_server_lacks(run, server, data, capsys):
    from blake3 import blake3
    staging = StagingServer(server, held={blake3(b"x,y\n1,2\n").hexdigest()})           # a.csv is already there
    assert run("run", "Vision", "--walltime", "01:00", "--data", str(data)) == 0
    assert [content for content, _, _ in staging.uploaded.values()] == [b"3,4\n"]       # only sub/b.csv was sent
    assert all(method == "PUT" for _, _, method in staging.uploaded.values())
    out = capsys.readouterr().out
    assert "uploading sub/b.csv" in out and "uploaded 1, already on the server 1" in out
    body = server.body(-1)
    assert [(f["path"], f["size_bytes"]) for f in body["dataset"]] == [("a.csv", 8), ("sub/b.csv", 4)]
    assert all(len(f["blake3"]) == 64 for f in body["dataset"])


def test_a_repeat_run_on_the_same_data_sends_nothing(run, server, data):
    staging = StagingServer(server)
    run("run", "Vision", "--walltime", "01:00", "--data", str(data))
    first = len(staging.uploaded)
    run("run", "Vision", "--walltime", "01:00", "--data", str(data))
    assert first == 2 and len(staging.uploaded) == 2


def test_the_dataset_is_staged_before_the_job_is_submitted(run, server, data):
    StagingServer(server)
    run("run", "Vision", "--walltime", "01:00", "--data", str(data))
    paths = [r.url.path for r in server.seen if r.method in ("POST", "PUT")]
    assert paths.index(f"{BASE}/runs") == len(paths) - 1


def test_a_bad_dataset_stops_the_run_before_anything_is_sent(run, server, tmp_path, capsys):
    assert run("run", "Vision", "--walltime", "01:00", "--data", str(tmp_path / "missing")) == 2
    assert "not a file or folder" in capsys.readouterr().err and server.seen == []


def test_the_servers_complaint_about_unstaged_data_is_shown(run, server, data, capsys):
    StagingServer(server)
    server.on("POST", f"{BASE}/runs", 409, {"detail": "These dataset files must be uploaded first: a.csv", "code": "DATASET_NOT_STAGED"})
    assert run("run", "Vision", "--walltime", "01:00", "--data", str(data)) == 1
    assert "must be uploaded first: a.csv" in capsys.readouterr().err


def test_hyperparameter_values_are_read_as_json_when_they_can_be():
    assert parse_hyperparameters(["a=1", "b=2.5", "c=true", "d=null", "e=text", 'f="quoted"', "g={\"k\":1}", "h=a=b"]) == {
        "a": 1, "b": 2.5, "c": True, "d": None, "e": "text", "f": "quoted", "g": {"k": 1}, "h": "a=b"}


# ── rebuild and compare ───────────────────────────────────────────────

REBUILT = run_json("A run 2", status="QUEUED", job_state="IN_PROCESS", rebuild_of="A run 1")


def comparison(**over):
    base = {"rebuilt": "A run 2", "original": "A run 1", "rebuilt_status": "SUCCEEDED", "tolerance": 0.0,
            "metrics": [{"name": "loss", "step": 1, "original": 1.0, "rebuilt": 1.0, "difference": 0.0, "within_tolerance": True},
                        {"name": "loss", "step": 2, "original": 0.5, "rebuilt": 0.5, "difference": 0.0, "within_tolerance": True}],
            "metrics_match": True, "reproduced": True,
            "weights": {"original_blake3": "ab" * 32, "rebuilt_blake3": "ab" * 32, "identical": True}}
    return {**base, **over}


def test_a_rebuild_sends_only_the_original_reference_and_the_data(run, server, data, capsys):
    StagingServer(server)
    server.on("POST", f"{BASE}/runs", 201, REBUILT)
    assert run("run", "Vision", "--walltime", "02:00", "--gpus", "2", "--rebuild", "A/1", "--data", str(data)) == 0
    body = server.body(-1)
    assert body["rebuild_of"] == "A/1" and (body["gpus"], body["walltime"]) == (2, "02:00")
    assert not {"hyperparameters", "entrypoint", "version"} & set(body)
    out = capsys.readouterr().out
    assert "Queued A run 2, a rebuild of A run 1 (job 41)" in out
    assert "prayog project compare Vision A/2" in out


@pytest.mark.parametrize("extra", [["--hp", "lr=1"], ["--version", "B"], ["--entrypoint", "x.py"]])
def test_a_rebuild_refuses_options_that_would_change_the_original(run, server, capsys, extra):
    assert run("run", "Vision", "--walltime", "01:00", "--rebuild", "A/1", *extra) == 2
    assert "uses the original run's version" in capsys.readouterr().err and server.seen == []


def test_a_rejected_rebuild_shows_the_servers_reason(run, server, capsys):
    server.on("POST", f"{BASE}/runs", 404, {"detail": "This run cannot be rebuilt: it was not found, or has no verified snapshot.",
                                            "code": "REBUILD_REJECTED"})
    assert run("run", "Vision", "--walltime", "01:00", "--rebuild", "A/9") == 1
    assert "cannot be rebuilt" in capsys.readouterr().err


def test_changed_data_is_explained(run, server, data, capsys):
    StagingServer(server)
    server.on("POST", f"{BASE}/runs", 409, {"detail": "A rebuild must use exactly the original run's data (checked by BLAKE3). "
                                                      "different from the original: a.csv", "code": "DATASET_MISMATCH"})
    assert run("run", "Vision", "--walltime", "01:00", "--rebuild", "A/1", "--data", str(data)) == 1
    assert "different from the original: a.csv" in capsys.readouterr().err


def test_a_rebuild_is_labelled_in_the_status(run, server, capsys):
    server.on("GET", f"{BASE}/status", 200, summary(latest_run=REBUILT))
    run("project", "status", "Vision")
    assert "A run 2: QUEUED (IN_PROCESS), a rebuild of A run 1" in capsys.readouterr().out


def test_compare_of_a_faithful_rebuild(run, server, capsys):
    server.on("GET", f"{BASE}/runs/A/2/compare", 200, comparison())
    assert run("project", "compare", "Vision", "A/2") == 0
    out = capsys.readouterr().out
    assert "A run 2 (SUCCEEDED) against A run 1, tolerance 0:" in out
    assert "loss" in out and out.count("ok") == 2
    assert "identical, byte for byte (BLAKE3 abababababab)" in out
    assert out.strip().endswith("Result: REPRODUCED")
    assert server.seen[-1].url.params["tolerance"] == "0.0"


def test_compare_sends_the_tolerance_and_flags_what_differs(run, server, capsys):
    bad = comparison(
        tolerance=0.01, reproduced=False, metrics_match=False,
        metrics=[{"name": "loss", "step": 1, "original": 1.0, "rebuilt": 1.5, "difference": 0.5, "within_tolerance": False},
                 {"name": "acc", "step": 1, "original": 0.9, "rebuilt": None, "difference": None, "within_tolerance": False}],
        weights={"original_blake3": "ab" * 32, "rebuilt_blake3": "cd" * 32, "identical": False})
    server.on("GET", f"{BASE}/runs/A/2/compare", 200, bad)
    assert run("project", "compare", "Vision", "A/2", "--tolerance", "0.01") == 0
    assert server.seen[-1].url.params["tolerance"] == "0.01"
    out = capsys.readouterr().out
    assert out.count("DIFFERS") == 2 and "Weights: different" in out and "Result: NOT reproduced" in out
    assert [l.split()[:5] for l in out.splitlines() if l.startswith("acc")] == [["acc", "1", "0.9", "-", "-"]]


def test_matching_metrics_with_different_weights_is_reproduced_with_a_caveat(run, server, capsys):
    server.on("GET", f"{BASE}/runs/A/2/compare", 200, comparison(
        weights={"original_blake3": "ab" * 32, "rebuilt_blake3": "cd" * 32, "identical": False}))
    run("project", "compare", "Vision", "A/2", "--tolerance", "1e-3")
    assert "REPRODUCED (metrics match, weights differ" in capsys.readouterr().out


def test_compare_when_nothing_was_saved(run, server, capsys):
    server.on("GET", f"{BASE}/runs/A/2/compare", 200, comparison(
        metrics=[], metrics_match=False, reproduced=False,
        weights={"original_blake3": None, "rebuilt_blake3": None, "identical": False}))
    run("project", "compare", "Vision", "A/2")
    out = capsys.readouterr().out
    assert "logged no metrics" in out and "neither run saved a checkpoint" in out


@pytest.mark.parametrize("args", [["A2"], ["A/x"], ["A/2", "--tolerance", "-1"]])
def test_compare_checks_its_arguments_locally(run, server, args):
    assert run("project", "compare", "Vision", *args) == 2
    assert [r for r in server.seen if "compare" in r.url.path] == []


def test_compare_of_something_that_is_not_a_rebuild_shows_the_servers_reason(run, server, capsys):
    server.on("GET", f"{BASE}/runs/A/1/compare", 400, {"detail": "A run 1 is not a rebuild, so there is nothing to compare it with."})
    assert run("project", "compare", "Vision", "A/1") == 1
    assert "not a rebuild" in capsys.readouterr().err


# ── download ──────────────────────────────────────────────────────────

class ResultsServer:
    """A finished run's files and records, served from memory through the fake server."""

    FILES = {"ck1": b"weights-one", "ck2": b"weights-two", "ck3": b"weights-three", "log1": b"epoch 1\nepoch 2\n"}

    def __init__(self, server, tamper=None, log=True):
        import hashlib
        self.server, self.tamper = server, tamper or {}
        checkpoints = [{"step": 1, "is_best": False, "artifact_id": "ck1", "size_bytes": 11},
                       {"step": 2, "is_best": True, "artifact_id": "ck2", "size_bytes": 11},
                       {"step": 3, "is_best": False, "artifact_id": "ck3", "size_bytes": 13}]
        self.run = run_json(status="SUCCEEDED", job_state="COMPLETED", checkpoints=checkpoints,
                            logs_artifact_id="log1" if log else None)
        server.on("GET", f"{BASE}/runs/A/1", 200, self.run)
        server.on("GET", f"{BASE}/versions", 200, [version("A", 1, current=True, head=True)])
        server.on("GET", "/mlflow/experiments/1/artifacts", 200, [
            {"id": k, "sha256": hashlib.sha256(v).hexdigest(), "size_bytes": len(v)} for k, v in self.FILES.items()])
        server.on("GET", "/mlflow/runs/1/metrics", 200, [{"name": "loss", "step": 1, "value": 0.9},
                                                       {"name": "loss", "step": 2, "value": 0.4}])
        self.inner = server.handler
        server.handler = self.handler

    def handler(self, request):
        if request.url.path.startswith("/mlflow/artifacts/") and request.url.path.endswith("/content"):
            self.server.seen.append(request)
            key = request.url.path.split("/")[3]
            return httpx.Response(200, content=self.tamper.get(key, self.FILES[key]))
        return self.inner(request)


def test_download_saves_the_best_checkpoint_log_and_metrics_by_default(run, server, tmp_path, capsys):
    ResultsServer(server)
    assert run("project", "download", "Vision", "A/1", "--to", str(tmp_path / "out")) == 0
    out = tmp_path / "out"
    assert sorted(p.name for p in (out / "checkpoints").iterdir()) == ["step-2-best"]
    assert (out / "checkpoints" / "step-2-best").read_bytes() == b"weights-two"
    assert (out / "run.log").read_bytes() == b"epoch 1\nepoch 2\n"
    assert json.loads((out / "metrics.json").read_text()) == [{"name": "loss", "step": 1, "value": 0.9},
                                                              {"name": "loss", "step": 2, "value": 0.4}]
    text = capsys.readouterr().out
    assert "Saved A run 1" in text and "checkpoints/step-2-best" in text and "metrics.json (2 values)" in text
    assert list(out.rglob("*.part")) == []


@pytest.mark.parametrize("which,expected", [("all", ["step-1", "step-2-best", "step-3"]), ("last", ["step-3"]),
                                            ("1", ["step-1"]), ("none", [])])
def test_download_can_choose_which_checkpoints(run, server, tmp_path, which, expected):
    ResultsServer(server)
    assert run("project", "download", "Vision", "A/1", "--to", str(tmp_path / "o"), "--checkpoint", which) == 0
    folder = tmp_path / "o" / "checkpoints"
    assert sorted(p.name for p in folder.iterdir()) == expected if folder.exists() else expected == []


def test_download_of_a_step_the_run_lacks_lists_the_steps(run, server, tmp_path, capsys):
    ResultsServer(server)
    assert run("project", "download", "Vision", "A/1", "--to", str(tmp_path / "o"), "--checkpoint", "9") == 2
    assert "no checkpoint at step 9. Its steps are: 1, 2, 3." in capsys.readouterr().err


def test_download_without_a_log_or_metrics(run, server, tmp_path):
    ResultsServer(server)
    run("project", "download", "Vision", "A/1", "--to", str(tmp_path / "o"), "--no-log", "--no-metrics", "--checkpoint", "last")
    assert sorted(p.name for p in (tmp_path / "o").rglob("*") if p.is_file()) == ["step-3"]


def test_a_run_with_no_log_says_so(run, server, tmp_path, capsys):
    ResultsServer(server, log=False)
    assert run("project", "download", "Vision", "A/1", "--to", str(tmp_path / "o")) == 0
    assert "no log yet" in capsys.readouterr().out and not (tmp_path / "o" / "run.log").exists()


def test_a_download_that_fails_its_checksum_is_not_saved(run, server, tmp_path, capsys):
    ResultsServer(server, tamper={"ck2": b"corrupted!!"})              # same length, different bytes
    assert run("project", "download", "Vision", "A/1", "--to", str(tmp_path / "o")) == 1
    assert "failed its checksum" in capsys.readouterr().err
    assert not (tmp_path / "o" / "checkpoints" / "step-2-best").exists()
    assert list((tmp_path / "o").rglob(".*part")) == []


def test_a_download_of_the_wrong_length_is_not_saved(run, server, tmp_path):
    ResultsServer(server, tamper={"ck2": b"short"})
    assert run("project", "download", "Vision", "A/1", "--to", str(tmp_path / "o")) == 1
    assert not (tmp_path / "o" / "checkpoints" / "step-2-best").exists()


def test_existing_files_are_never_overwritten(run, server, tmp_path, capsys):
    ResultsServer(server)
    target = tmp_path / "o"
    (target / "checkpoints").mkdir(parents=True)
    (target / "checkpoints" / "step-2-best").write_bytes(b"mine")
    assert run("project", "download", "Vision", "A/1", "--to", str(target)) == 2
    assert "never overwritten" in capsys.readouterr().err
    assert (target / "checkpoints" / "step-2-best").read_bytes() == b"mine"


def test_download_needs_a_run_reference(run, server, tmp_path):
    assert run("project", "download", "Vision", "A1", "--to", str(tmp_path / "o")) == 2
    assert server.seen == []


def test_download_of_a_missing_run_shows_the_servers_reason(run, server, tmp_path, capsys):
    server.on("GET", f"{BASE}/runs/A/7", 404, {"detail": "Version A has no run 7."})
    assert run("project", "download", "Vision", "A/7", "--to", str(tmp_path / "o")) == 1
    assert "no run 7" in capsys.readouterr().err
