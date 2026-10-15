import argparse
import hashlib
import json
import os
import re
import shutil
import select
import stat
import threading
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy.orm import Session

from database import SessionLocal
from logic.artifact_storage import ARTIFACT_ROOT, delete_artifact, persist_artifact
from logic.artifacts import MAX_ARTIFACT_BYTES, get_verified_artifact
from logic.errors import BadRequestError, NotFoundError
from logic.mlflow import finish_run, update_rebuild
from mlflow_models import Artifact, ModelRun, ModelSnapshot, RebuildAttempt
from models import User

MAX_ARCHIVE_FILES = 10000
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024
MAX_RUNTIME_SECONDS = 1800
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_IMAGE = "prayog-ml-runtime:python3.12"
RUNNER_SOURCE = "import os,subprocess,sys,time; env=os.environ.copy(); token=env.pop('PRAYOG_COMPLETION_TOKEN'); result=subprocess.run([sys.executable,env['PRAYOG_ENTRYPOINT']],env=env); print('__PRAYOG_EXIT_'+token+'_'+str(result.returncode)+'__',flush=True); time.sleep(3600)"
IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def configured_timeout():
    try:
        value = int(os.getenv("PRAYOG_WORKER_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(1, min(value, MAX_RUNTIME_SECONDS))


def configured_image():
    value = os.getenv("PRAYOG_WORKER_IMAGE", DEFAULT_IMAGE)
    if not IMAGE_PATTERN.fullmatch(value):
        raise RuntimeError("PRAYOG_WORKER_IMAGE contains invalid characters")
    return value


def claim_next_rebuild(db: Session, worker_id: str) -> int | None:
    attempt = (
        db.query(RebuildAttempt)
        .filter(
            RebuildAttempt.state == "REBUILD_INITIATED",
            RebuildAttempt.worker_id.is_(None),
        )
        .order_by(RebuildAttempt.created_at, RebuildAttempt.id)
        .with_for_update(skip_locked=True)
        .first()
    )
    if attempt is None:
        db.rollback()
        return None
    attempt.worker_id = worker_id
    attempt.worker_claimed_at = utcnow()
    rebuild_id = attempt.id
    db.commit()
    return rebuild_id


def claim_next_first_time_run(db: Session, worker_id: str) -> int | None:
    run = (
        db.query(ModelRun)
        .filter(
            ModelRun.status == "QUEUED",
            ModelRun.run_type == "FIRST_TIME",
            ModelRun.code_artifact_id.is_not(None),
            ModelRun.worker_id.is_(None),
        )
        .order_by(ModelRun.started_at, ModelRun.id)
        .with_for_update(skip_locked=True)
        .first()
    )
    if run is None:
        db.rollback()
        return None
    run.status = "RUNNING"
    run.started_at = utcnow()
    run.worker_id = worker_id
    run.worker_claimed_at = run.started_at
    run_id = run.id
    db.commit()
    return run_id


def claim_next_job(db: Session, worker_id: str) -> tuple[str, int] | None:
    # Compare both job classes on their queue-entry timestamp. Neither rebuilds
    # nor first-time runs get a class-based priority advantage.
    for _ in range(5):
        run = (
            db.query(ModelRun)
            .filter(
                ModelRun.status == "QUEUED",
                ModelRun.run_type == "FIRST_TIME",
                ModelRun.code_artifact_id.is_not(None),
                ModelRun.worker_id.is_(None),
            )
            .order_by(ModelRun.started_at, ModelRun.id)
            .first()
        )
        attempt = (
            db.query(RebuildAttempt)
            .filter(
                RebuildAttempt.state == "REBUILD_INITIATED",
                RebuildAttempt.worker_id.is_(None),
            )
            .order_by(RebuildAttempt.initiated_at, RebuildAttempt.id)
            .first()
        )
        candidates = []
        if run is not None:
            candidates.append((run.started_at or run.created_at, run.id, "FIRST_TIME"))
        if attempt is not None:
            candidates.append((attempt.initiated_at or attempt.created_at, attempt.id, "REBUILD"))
        if not candidates:
            db.rollback()
            return None

        _, job_id, job_type = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        now = utcnow()
        if job_type == "FIRST_TIME":
            claimed = (
                db.query(ModelRun)
                .filter(
                    ModelRun.id == job_id,
                    ModelRun.status == "QUEUED",
                    ModelRun.run_type == "FIRST_TIME",
                    ModelRun.code_artifact_id.is_not(None),
                    ModelRun.worker_id.is_(None),
                )
                .with_for_update(skip_locked=True)
                .first()
            )
            if claimed is not None:
                claimed.status = "RUNNING"
                claimed.started_at = now
                claimed.worker_id = worker_id
                claimed.worker_claimed_at = now
        else:
            claimed = (
                db.query(RebuildAttempt)
                .filter(
                    RebuildAttempt.id == job_id,
                    RebuildAttempt.state == "REBUILD_INITIATED",
                    RebuildAttempt.worker_id.is_(None),
                )
                .with_for_update(skip_locked=True)
                .first()
            )
            if claimed is not None:
                claimed.worker_id = worker_id
                claimed.worker_claimed_at = now
        if claimed is not None:
            db.commit()
            return job_type, job_id
        db.rollback()

    return None


def recover_stale_claims(db: Session, timeout_seconds: int) -> int:
    cutoff = utcnow() - timedelta(seconds=timeout_seconds + 300)
    stale = (
        db.query(RebuildAttempt)
        .filter(
            RebuildAttempt.state == "REBUILD_INITIATED",
            RebuildAttempt.worker_id.is_not(None),
            RebuildAttempt.worker_claimed_at < cutoff,
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    now = utcnow()
    recovered = 0
    for attempt in stale:
        execution = (
            db.query(ModelRun)
            .filter(
                ModelRun.id == attempt.execution_run_id,
                ModelRun.user_id == attempt.user_id,
            )
            .with_for_update()
            .first()
        )
        attempt.state = "REBUILD_FAILED"
        attempt.failure_summary = "The execution worker stopped responding before reporting a result."
        attempt.finished_at = now
        if execution is not None and execution.status == "RUNNING":
            execution.status = "FAILED"
            execution.error_summary = attempt.failure_summary
            execution.finished_at = now
            execution.logging_state = "DEGRADED"
            execution.logging_warnings = ["The worker ended without a final artifact report."]
        recovered += 1
    stale_runs = (
        db.query(ModelRun)
        .filter(
            ModelRun.status == "RUNNING",
            ModelRun.run_type == "FIRST_TIME",
            ModelRun.worker_id.is_not(None),
            ModelRun.worker_claimed_at < cutoff,
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    for run in stale_runs:
        run.status = "FAILED"
        run.error_summary = "The execution worker stopped responding before reporting a result."
        run.finished_at = now
        run.logging_state = "DEGRADED"
        run.logging_warnings = ["The worker ended without a final artifact report."]
        recovered += 1
    if recovered:
        db.commit()
    else:
        db.rollback()
    return recovered


def extract_code_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    total_size = 0
    seen = set()
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if not members or len(members) > MAX_ARCHIVE_FILES:
            raise ValueError("The code archive has an invalid number of entries")
        for member in members:
            name = member.filename
            relative = PurePosixPath(name)
            if not name or "\\" in name or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("The code archive contains an unsafe path")
            if name in seen:
                raise ValueError("The code archive contains duplicate paths")
            seen.add(name)
            if member.flag_bits & 1:
                raise ValueError("Encrypted code archives are not supported")
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("The code archive contains a special file")
            if stat.S_ISLNK(mode):
                raise ValueError("The code archive contains a symbolic link")
            if member.file_size < 0:
                raise ValueError("The code archive contains an invalid file size")
            total_size += member.file_size
            if total_size > MAX_EXTRACTED_BYTES or member.file_size > MAX_ARTIFACT_BYTES:
                raise ValueError("The expanded code archive exceeds the size limit")
            target = (destination / Path(*relative.parts)).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError("The code archive contains a path traversal")
            if member.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.open(member, "r") as source, target.open("xb") as output:
                copied = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > member.file_size or copied > MAX_ARTIFACT_BYTES:
                        raise ValueError("The code archive expanded beyond its declared size")
                    output.write(chunk)
                if copied != member.file_size:
                    raise ValueError("The code archive entry was truncated")
            target.chmod(0o444)
    for directory, subdirectories, _ in os.walk(destination, topdown=False):
        for subdirectory in subdirectories:
            (Path(directory) / subdirectory).chmod(0o555)
        Path(directory).chmod(0o555)


def validate_entrypoint(workspace: Path, value) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("The rebuild entrypoint must be a relative Python file path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or "\\" in value or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("The rebuild entrypoint must be a safe relative path")
    if relative.suffix.lower() != ".py":
        raise ValueError("The rebuild entrypoint must be a Python file")
    entrypoint = (workspace / Path(*relative.parts)).resolve()
    if not entrypoint.is_relative_to(workspace.resolve()) or not entrypoint.is_file():
        raise ValueError("The configured rebuild entrypoint does not exist in the code archive")
    return relative


def prepare_input_directory(input_dir: Path, weights_path: Path | None = None) -> None:
    input_dir.mkdir(mode=0o700)
    if weights_path is not None:
        shutil.copyfile(weights_path, input_dir / "weights.bin")
        os.chmod(input_dir / "weights.bin", 0o444)
    os.chmod(input_dir, 0o555)


def build_docker_command(
    image: str,
    container_name: str,
    workspace: Path,
    input_dir: Path,
    entrypoint: Path,
    run_id: int,
    snapshot_id: str,
    hyperparameters: dict,
    has_weights: bool,
    completion_token: str,
) -> list[str]:
    command = [
        "docker", "create", "--pull=never",
        "--name", container_name,
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--memory", "2g",
        "--cpus", "2",
        "--user", "65534:65534",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
        "--ulimit", "nofile=1024:1024",
        "--mount", f"type=bind,source={workspace},target=/workspace,readonly",
        "--mount", "type=tmpfs,destination=/output,tmpfs-size=314572800,tmpfs-mode=1777",
        "--mount", f"type=bind,source={input_dir},target=/input,readonly",
        "--workdir", "/workspace",
        "--env", "HOME=/tmp",
        "--env", "TMPDIR=/tmp",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTHONUNBUFFERED=1",
        "--env", f"PRAYOG_ENTRYPOINT=/workspace/{entrypoint.as_posix()}",
        "--env", f"PRAYOG_COMPLETION_TOKEN={completion_token}",
        "--env", f"PRAYOG_RUN_ID={run_id}",
        "--env", f"PRAYOG_SNAPSHOT_ID={snapshot_id}",
        "--env", f"PRAYOG_HYPERPARAMETERS_JSON={json.dumps(hyperparameters, separators=(',', ':'))}",
        "--env", f"PRAYOG_INPUT_WEIGHTS={'/input/weights.bin' if has_weights else ''}",
        image,
        "python",
        "-c",
        RUNNER_SOURCE,
    ]
    return command


def copy_container_output(container_name: str, destination: Path, log_path: Path) -> bool:
    script = (
        "import os,stat,sys\n"
        "fd=os.open('/output/weights.bin',os.O_RDONLY|os.O_NOFOLLOW)\n"
        "metadata=os.fstat(fd)\n"
        f"if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size <= 0 or metadata.st_size > {MAX_ARTIFACT_BYTES}: raise SystemExit(2)\n"
        "while True:\n"
        " chunk=os.read(fd,1048576)\n"
        " if not chunk: break\n"
        " sys.stdout.buffer.write(chunk)\n"
    )
    try:
        process = subprocess.Popen(
            ["docker", "exec", container_name, "python", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError:
        return False
    written = 0
    deadline = time.monotonic() + 30
    try:
        with destination.open("xb") as output:
            while True:
                ready, _, _ = select.select([process.stdout], [], [], 0.1)
                if not ready:
                    if time.monotonic() >= deadline:
                        process.kill()
                        process.wait()
                        raise TimeoutError("Container output collection timed out")
                    continue
                chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ARTIFACT_BYTES:
                    process.kill()
                    process.wait()
                    raise ValueError("Container output exceeded the artifact size limit")
                output.write(chunk)
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise TimeoutError("Container output collection timed out")
        error = process.stderr.read()[-2048:] if process.stderr else b""
        if return_code != 0 or written <= 0:
            destination.unlink(missing_ok=True)
            with log_path.open("ab") as output:
                output.write(b"\n[Worker note: output weights were absent or invalid: ")
                output.write(error)
                output.write(b"]\n")
            return False
        os.chmod(destination, 0o600)
        return True
    except (OSError, TimeoutError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        with log_path.open("ab") as output:
            output.write(f"\n[Worker note: output collection failed ({type(exc).__name__}).]\n".encode())
        return False
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def run_container(
    command: list[str],
    container_name: str,
    log_path: Path,
    timeout_seconds: int,
    weights_destination: Path,
    completion_token: str,
) -> tuple[int, bool, bool]:
    try:
        created = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_path.write_text(
            f"Worker could not create the execution container: {type(exc).__name__}\n",
            encoding="utf-8",
        )
        return 127, False, False
    if created.returncode != 0:
        details = (created.stderr or b"")[-MAX_LOG_BYTES:]
        log_path.write_bytes(details or b"Worker could not create the execution container.\n")
        return created.returncode or 127, False, False

    written = 0
    truncated = False
    timed_out = False
    weights_copied = False
    app_return_code = None
    completed = threading.Event()
    process = None
    try:
        try:
            process = subprocess.Popen(
                ["docker", "start", "--attach", container_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
        except OSError as exc:
            log_path.write_text(
                f"Worker could not start the execution container: {type(exc).__name__}\n",
                encoding="utf-8",
            )
            return 127, False, False

        def collect_output():
            nonlocal written, truncated, app_return_code
            tail = b""
            with log_path.open("wb") as output:
                while True:
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if not chunk:
                        break
                    available = MAX_LOG_BYTES - written
                    if available > 0:
                        output.write(chunk[:available])
                        written += min(len(chunk), available)
                    if len(chunk) > available:
                        truncated = True
                    combined = tail + chunk
                    match = re.search(
                        rb"__PRAYOG_EXIT_" + completion_token.encode("ascii") + rb"_(-?\d+)__",
                        combined,
                    )
                    if match is not None:
                        app_return_code = int(match.group(1))
                        completed.set()
                    tail = combined[-256:]

        reader = threading.Thread(target=collect_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout_seconds
        while not completed.is_set():
            if process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                try:
                    subprocess.run(
                        ["docker", "kill", container_name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
                break
            time.sleep(0.1)

        if completed.is_set() and not timed_out:
            weights_copied = copy_container_output(container_name, weights_destination, log_path)
            try:
                subprocess.run(
                    ["docker", "stop", "--time", "1", container_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif not timed_out:
            try:
                subprocess.run(
                    ["docker", "kill", container_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=5)
        if reader.is_alive():
            process.kill()
            reader.join(timeout=1)
        if truncated:
            with log_path.open("ab") as output:
                output.write(b"\n[Worker note: output truncated at the configured log limit.]\n")
        if timed_out:
            with log_path.open("ab") as output:
                output.write(b"\n[Worker note: execution exceeded the time limit.]\n")
        return_code = app_return_code if app_return_code is not None else 127
        return return_code, timed_out, weights_copied
    finally:
        try:
            subprocess.run(
                ["docker", "rm", "--force", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

def safe_output_file(output_dir: Path, filename: str) -> Path | None:
    candidate = output_dir / filename
    if candidate.is_symlink() or not candidate.exists():
        return None
    resolved = candidate.resolve()
    if resolved.parent != output_dir.resolve():
        return None
    metadata = candidate.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return None
    if metadata.st_size <= 0 or metadata.st_size > MAX_ARTIFACT_BYTES:
        return None
    return candidate


def store_worker_artifact(db: Session, user: User, snapshot: ModelSnapshot | ModelRun, kind: str, source: Path) -> str:
    size = source.stat().st_size
    if size <= 0 or size > MAX_ARTIFACT_BYTES:
        raise ValueError("Worker artifact is empty or exceeds the configured limit")
    artifact_id = str(uuid4())
    storage_key = str(uuid4())
    digest = hashlib.sha256()
    with source.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    persisted = False
    try:
        persist_artifact(source, storage_key)
        persisted = True
        artifact = Artifact(
            id=artifact_id,
            user_id=user.id,
            experiment_id=snapshot.experiment_id,
            project_id=snapshot.project_id,
            model_id=snapshot.model_id,
            kind=kind,
            storage_key=storage_key,
            sha256=digest.hexdigest(),
            size_bytes=size,
        )
        db.add(artifact)
        db.commit()
        return artifact_id
    except Exception:
        db.rollback()
        if persisted:
            try:
                delete_artifact(storage_key)
            except Exception:
                pass
        raise


def execute_rebuild(db: Session, rebuild_id: int, worker_id: str, timeout_seconds: int, image: str) -> None:
    attempt = (
        db.query(RebuildAttempt)
        .filter(
            RebuildAttempt.id == rebuild_id,
            RebuildAttempt.state == "REBUILD_INITIATED",
            RebuildAttempt.worker_id == worker_id,
        )
        .first()
    )
    if attempt is None:
        db.rollback()
        return
    user = db.query(User).filter(User.id == attempt.user_id).first()
    snapshot = (
        db.query(ModelSnapshot)
        .filter(
            ModelSnapshot.snapshot_id == attempt.snapshot_id,
            ModelSnapshot.user_id == attempt.user_id,
        )
        .first()
    )
    if user is None or snapshot is None:
        raise NotFoundError("The rebuild's owner or source snapshot is missing")

    log_path = None
    weights_artifact_id = None
    logs_artifact_id = None
    failure_summary = None
    terminal_state = "REBUILD_FAILED"
    try:
        code_artifact, code_path = get_verified_artifact(
            db, snapshot.code_artifact_id, user.id, snapshot.experiment_id, "CODE"
        )
        if snapshot.code_sha256 and code_artifact.sha256 != snapshot.code_sha256:
            raise BadRequestError("Snapshot code digest does not match its artifact")
        weights_path = None
        if snapshot.weights_artifact_id:
            _, weights_path = get_verified_artifact(
                db, snapshot.weights_artifact_id, user.id, snapshot.experiment_id, "WEIGHTS"
            )
        with tempfile.TemporaryDirectory(prefix="prayog-worker-") as temporary:
            root = Path(temporary)
            archive_path = root / "source.zip"
            shutil.copyfile(code_path, archive_path)
            workspace = root / "workspace"
            extract_code_archive(archive_path, workspace)
            entrypoint = validate_entrypoint(workspace, attempt.environment.get("entrypoint", "main.py"))
            weights_destination = root / "weights.bin"
            input_dir = root / "input"
            prepare_input_directory(input_dir, weights_path)
            log_path = root / "execution.log"
            container_name = f"prayog-rebuild-{rebuild_id}-{uuid4().hex[:8]}"
            completion_token = uuid4().hex
            command = build_docker_command(
                image, container_name, workspace, input_dir,
                entrypoint, attempt.execution_run_id, snapshot.snapshot_id,
                attempt.environment.get("hyperparameters", snapshot.hyperparameters),
                weights_path is not None, completion_token,
            )
            return_code, timed_out, weights_copied = run_container(
                command, container_name, log_path, timeout_seconds, weights_destination, completion_token
            )
            if log_path.is_file() and log_path.stat().st_size:
                logs_artifact_id = store_worker_artifact(db, user, snapshot, "LOG", log_path)
            if timed_out:
                failure_summary = "Rebuild execution exceeded the configured time limit."
            elif return_code != 0:
                failure_summary = f"Rebuild execution failed with exit code {return_code}."
            else:
                if weights_copied:
                    produced_weights = safe_output_file(root, "weights.bin")
                    if produced_weights is None:
                        failure_summary = "Rebuild produced an invalid or oversized weights artifact."
                    else:
                        weights_artifact_id = store_worker_artifact(db, user, snapshot, "WEIGHTS", produced_weights)
                if failure_summary is None:
                    terminal_state = "REBUILD_SUCCESS"
    except (BadRequestError, NotFoundError, ValueError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        failure_summary = f"Rebuild worker rejected or could not complete the job ({type(exc).__name__})."
    except Exception as exc:
        failure_summary = f"Rebuild worker encountered an internal error ({type(exc).__name__})."

    if terminal_state != "REBUILD_SUCCESS" and not failure_summary:
        failure_summary = "Rebuild worker could not complete the job."
    if terminal_state == "REBUILD_SUCCESS":
        update_rebuild(db, rebuild_id, user, terminal_state, logs_artifact_id, weights_artifact_id, None, worker_id=worker_id)
    else:
        update_rebuild(db, rebuild_id, user, "REBUILD_FAILED", logs_artifact_id, None, failure_summary, worker_id=worker_id)



def execute_first_time_run(
    db: Session, run_id: int, worker_id: str, timeout_seconds: int, image: str
) -> None:
    run = (
        db.query(ModelRun)
        .filter(
            ModelRun.id == run_id,
            ModelRun.status == "RUNNING",
            ModelRun.run_type == "FIRST_TIME",
            ModelRun.worker_id == worker_id,
        )
        .first()
    )
    if run is None:
        db.rollback()
        return
    user = db.query(User).filter(User.id == run.user_id).first()
    if user is None:
        raise NotFoundError("The first-time run owner is missing")

    logs_artifact_id = None
    weights_artifact_id = run.weights_artifact_id
    failure_summary = None
    terminal_status = "FAILED"
    try:
        code_artifact, code_path = get_verified_artifact(
            db, run.code_artifact_id, user.id, run.experiment_id, "CODE"
        )
        if run.code_snapshot_sha256 and code_artifact.sha256 != run.code_snapshot_sha256:
            raise BadRequestError("First-time run code digest does not match its artifact")
        weights_path = None
        if run.weights_artifact_id:
            _, weights_path = get_verified_artifact(
                db, run.weights_artifact_id, user.id, run.experiment_id, "WEIGHTS"
            )
        with tempfile.TemporaryDirectory(prefix="prayog-run-") as temporary:
            root = Path(temporary)
            archive_path = root / "source.zip"
            shutil.copyfile(code_path, archive_path)
            workspace = root / "workspace"
            extract_code_archive(archive_path, workspace)
            entrypoint = validate_entrypoint(workspace, run.environment.get("entrypoint", "main.py"))
            weights_destination = root / "weights.bin"
            input_dir = root / "input"
            prepare_input_directory(input_dir, weights_path)
            log_path = root / "execution.log"
            container_name = f"prayog-run-{run.id}-{uuid4().hex[:8]}"
            completion_token = uuid4().hex
            command = build_docker_command(
                image, container_name, workspace, input_dir,
                entrypoint, run.id, "", run.hyperparameters,
                weights_path is not None, completion_token,
            )
            return_code, timed_out, weights_copied = run_container(
                command, container_name, log_path, timeout_seconds, weights_destination, completion_token
            )
            if log_path.is_file() and log_path.stat().st_size:
                logs_artifact_id = store_worker_artifact(db, user, run, "LOG", log_path)
            if timed_out:
                failure_summary = "First-time execution exceeded the configured time limit."
            elif return_code != 0:
                failure_summary = f"First-time execution failed with exit code {return_code}."
            else:
                if weights_copied:
                    produced_weights = safe_output_file(root, "weights.bin")
                    if produced_weights is None:
                        failure_summary = "First-time execution produced an invalid or oversized weights artifact."
                    else:
                        weights_artifact_id = store_worker_artifact(db, user, run, "WEIGHTS", produced_weights)
                if failure_summary is None:
                    terminal_status = "SUCCEEDED"
    except (BadRequestError, NotFoundError, ValueError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        failure_summary = f"First-time execution could not be completed ({type(exc).__name__})."
    except Exception as exc:
        failure_summary = f"First-time execution encountered an internal error ({type(exc).__name__})."

    finish_run(
        db,
        run_id,
        user,
        {
            "status": terminal_status,
            "code_artifact_id": run.code_artifact_id,
            "weights_artifact_id": weights_artifact_id,
            "logs_artifact_id": logs_artifact_id,
            "error_summary": failure_summary,
        },
        worker_id=worker_id,
    )

def worker_loop(once: bool = False, poll_seconds: float = 2.0) -> int:
    worker_id = f"worker-{uuid4()}"
    timeout_seconds = configured_timeout()
    image = configured_image()
    while True:
        db = SessionLocal()
        try:
            recover_stale_claims(db, timeout_seconds)
            job = claim_next_job(db, worker_id)
        except Exception as exc:
            db.rollback()
            print(f"Worker queue error: {type(exc).__name__}", file=sys.stderr, flush=True)
            if once:
                return 1
            time.sleep(poll_seconds)
            continue
        finally:
            db.close()
        if job is None:
            if once:
                return 0
            time.sleep(poll_seconds)
            continue
        job_type, job_id = job
        db = SessionLocal()
        try:
            if job_type == "FIRST_TIME":
                execute_first_time_run(db, job_id, worker_id, timeout_seconds, image)
                print(f"First-time run {job_id} finished processing.", flush=True)
            else:
                execute_rebuild(db, job_id, worker_id, timeout_seconds, image)
                print(f"Rebuild {job_id} finished processing.", flush=True)
        except Exception as exc:
            db.rollback()
            print(f"{job_type} job {job_id} could not be finalized: {type(exc).__name__}", file=sys.stderr, flush=True)
        finally:
            db.close()
        if once:
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prayog isolated ML Flow rebuild worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued rebuild, then exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if args.poll_seconds < 0.2 or args.poll_seconds > 60:
        parser.error("--poll-seconds must be between 0.2 and 60")
    try:
        return worker_loop(once=args.once, poll_seconds=args.poll_seconds)
    except (RuntimeError, OSError) as exc:
        print(f"Worker configuration error: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
