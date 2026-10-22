"""
Project versions: the uploaded states of a project's folder, labelled A, B, C, ...

A version is a manifest artifact (the file inventory plus the ordered code chunks, written by
`prayog project link` / `update`). Its identity is the BLAKE3 digest the artifact store already
computed for the manifest bytes, so registering an unchanged folder finds the existing version
instead of creating another. Because chunks are content-addressed too, two versions that share
files share storage.

Two pointers, as in a migration tool:
    head      the newest version (nothing to store: the highest version_number)
    current   the version `prayog run` uses unless told otherwise (Project.current_version_id)
Registering a version makes it current; `set_current` moves the pointer back or forward.
"""

import json
import string
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from logic.artifacts import get_verified_artifact
from logic.errors import BadRequestError, NotFoundError
from mlflow_models import Artifact, ModelRun, Project, ProjectVersion
from models import User

MANIFEST_FORMAT = 1
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_CHUNKS = 4096
MAX_FILES = 20000
LETTERS = string.ascii_uppercase


def version_label(number: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA (spreadsheet-column style)."""
    if number < 1:
        raise ValueError("Version numbers start at 1")
    label = ""
    while number:
        number, rest = divmod(number - 1, 26)
        label = LETTERS[rest] + label
    return label


def parse_version(text: str) -> int:
    """The number for a label ('B', case-insensitive) or a plain number ('2'). BadRequestError otherwise."""
    value = (text or "").strip().upper()
    if value.isdigit() and int(value) >= 1:
        return int(value)
    if value and all(ch in LETTERS for ch in value):
        number = 0
        for ch in value:
            number = number * 26 + (LETTERS.index(ch) + 1)
        return number
    raise BadRequestError(f"'{text}' is not a version; use a letter such as A or B.")


@dataclass(frozen=True)
class VersionInfo:
    version: ProjectVersion
    label: str
    is_head: bool
    is_current: bool
    run_count: int


def _owned_project(db: Session, project_id: str, user: User, lock: bool = False) -> Project:
    query = db.query(Project).filter(Project.project_id == project_id, Project.user_id == user.id)
    if lock:
        query = query.with_for_update()
    project = query.first()
    if project is None:
        raise NotFoundError("Project not found")
    return project


def _validate_manifest(db: Session, user: User, project: Project, artifact: Artifact) -> int:
    """Check the manifest is well formed and every chunk it names is one of this project's artifacts.
    Returns the file count. A bad manifest is refused here, so a run can never be queued on one."""
    _, path = get_verified_artifact(db, artifact.id, user.id, artifact.experiment_id, "CODE")
    if artifact.size_bytes > MAX_MANIFEST_BYTES:
        raise BadRequestError("The version manifest is too large")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BadRequestError("The artifact is not a project version manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != MANIFEST_FORMAT:
        raise BadRequestError("The artifact is not a project version manifest")
    if manifest.get("experiment_id") != artifact.experiment_id:
        raise BadRequestError("The manifest belongs to another experiment")
    files, chunks = manifest.get("files"), manifest.get("chunks")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES or not all(
        isinstance(item, dict) and isinstance(item.get("path"), str) for item in files
    ):
        raise BadRequestError("The manifest's file list is invalid")
    if not isinstance(chunks, list) or not chunks or len(chunks) > MAX_CHUNKS:
        raise BadRequestError("The manifest's chunk list is invalid")
    ids = [chunk.get("artifact_id") if isinstance(chunk, dict) else None for chunk in chunks]
    rows = {
        row.id: row for row in db.query(Artifact).filter(
            Artifact.id.in_([i for i in ids if isinstance(i, str)]),
            Artifact.user_id == user.id,
            Artifact.project_id == project.project_id,
            Artifact.kind == "CODE",
        )
    }
    for chunk, artifact_id in zip(chunks, ids):
        row = rows.get(artifact_id)
        if row is None or row.sha256 != chunk.get("sha256") or row.size_bytes != chunk.get("size_bytes"):
            raise BadRequestError("The manifest names a code chunk that this project does not hold")
    return len(files)


def register_version(
    db: Session, user: User, project_id: str, manifest_artifact_id: str
) -> tuple[ProjectVersion, bool]:
    """Record the manifest as the project's next version and make it current. Idempotent: a manifest
    already registered returns its version (created=False) and is made current again."""
    project = _owned_project(db, project_id, user, lock=True)         # serialises numbering
    artifact = db.query(Artifact).filter(
        Artifact.id == manifest_artifact_id, Artifact.user_id == user.id, Artifact.project_id == project.project_id,
        Artifact.kind == "CODE",
    ).first()
    if artifact is None:
        raise NotFoundError("Manifest artifact not found")

    existing = db.query(ProjectVersion).filter(
        ProjectVersion.project_id == project.project_id, ProjectVersion.manifest_hash == artifact.storage_key
    ).first()
    if existing is not None:
        project.current_version_id = existing.id
        db.commit()
        db.refresh(existing)
        return existing, False

    file_count = _validate_manifest(db, user, project, artifact)
    head = db.query(ProjectVersion).filter(ProjectVersion.project_id == project.project_id) \
        .order_by(ProjectVersion.version_number.desc()).first()
    version = ProjectVersion(
        project_id=project.project_id, user_id=user.id, experiment_id=artifact.experiment_id,
        version_number=(head.version_number + 1) if head else 1,
        manifest_hash=artifact.storage_key, manifest_artifact_id=artifact.id,
        parent_version_id=head.id if head else None, file_count=file_count,
    )
    db.add(version)
    db.flush()
    project.current_version_id = version.id
    db.commit()
    db.refresh(version)
    return version, True


def get_version(db: Session, user: User, project_id: str, label: str) -> ProjectVersion:
    project = _owned_project(db, project_id, user)
    number = parse_version(label)
    version = db.query(ProjectVersion).filter(
        ProjectVersion.project_id == project.project_id, ProjectVersion.version_number == number
    ).first()
    if version is None:
        raise NotFoundError(f"This project has no version {version_label(number)}.")
    return version


def current_version(db: Session, user: User, project_id: str) -> ProjectVersion:
    project = _owned_project(db, project_id, user)
    if project.current_version_id is None:
        raise NotFoundError("This project has no uploaded version yet. Upload a folder with `prayog project link`.")
    return db.get(ProjectVersion, project.current_version_id)


def set_current(db: Session, user: User, project_id: str, label: str) -> ProjectVersion:
    project = _owned_project(db, project_id, user, lock=True)
    version = get_version(db, user, project_id, label)
    project.current_version_id = version.id
    db.commit()
    return version


def list_versions(db: Session, user: User, project_id: str) -> list[VersionInfo]:
    """Newest first, each with its run count and whether it is the head or the current version."""
    project = _owned_project(db, project_id, user)
    versions = db.query(ProjectVersion).filter(ProjectVersion.project_id == project.project_id) \
        .order_by(ProjectVersion.version_number.desc()).all()
    counts = dict(
        db.query(ModelRun.project_version_id, func.count(ModelRun.id))
        .filter(ModelRun.project_id == project.project_id, ModelRun.project_version_id.is_not(None))
        .group_by(ModelRun.project_version_id).all()
    )
    head_number = versions[0].version_number if versions else None
    return [
        VersionInfo(v, version_label(v.version_number), v.version_number == head_number,
                    v.id == project.current_version_id, counts.get(v.id, 0))
        for v in versions
    ]
