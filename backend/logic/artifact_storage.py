import os
import shutil
from pathlib import Path
from uuid import uuid4

from logic.errors import BadRequestError

ARTIFACT_ROOT = Path(
    os.getenv("PRAYOG_ARTIFACT_ROOT", Path(__file__).resolve().parents[1] / "artifacts")
).resolve()


def _validate_key(storage_key: str) -> str:
    if not storage_key or any(ch not in "0123456789abcdef-" for ch in storage_key.lower()):
        raise BadRequestError("Invalid artifact storage key")
    return storage_key


def _backend() -> str:
    value = os.getenv("PRAYOG_ARTIFACT_STORAGE", "local").strip().lower()
    if value not in {"local", "s3"}:
        raise RuntimeError("PRAYOG_ARTIFACT_STORAGE must be 'local' or 's3'")
    return value


def _local_path(storage_key: str) -> Path:
    path = (ARTIFACT_ROOT / _validate_key(storage_key)).resolve()
    if path.parent != ARTIFACT_ROOT:
        raise BadRequestError("Invalid artifact storage key")
    return path


def _cache_path(storage_key: str) -> Path:
    cache_root = (ARTIFACT_ROOT / ".s3-cache").resolve()
    if cache_root.parent != ARTIFACT_ROOT:
        raise BadRequestError("Invalid artifact cache path")
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(cache_root, 0o700)
    path = (cache_root / _validate_key(storage_key)).resolve()
    if path.parent != cache_root:
        raise BadRequestError("Invalid artifact storage key")
    return path


def _s3_config():
    bucket = os.getenv("PRAYOG_S3_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("PRAYOG_S3_BUCKET is required when PRAYOG_ARTIFACT_STORAGE=s3")
    prefix = os.getenv("PRAYOG_S3_PREFIX", "prayog-artifacts").strip("/")
    region = os.getenv("PRAYOG_S3_REGION") or os.getenv("AWS_DEFAULT_REGION")
    endpoint = os.getenv("PRAYOG_S3_ENDPOINT_URL") or None
    return bucket, prefix, region, endpoint


def _s3_client():
    import boto3

    _, _, region, endpoint = _s3_config()
    return boto3.client("s3", region_name=region, endpoint_url=endpoint)


def _object_key(storage_key: str) -> str:
    _, prefix, _, _ = _s3_config()
    key = _validate_key(storage_key)
    return f"{prefix}/{key}" if prefix else key


def persist_artifact(source: Path, storage_key: str) -> Path:
    _validate_key(storage_key)
    ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ARTIFACT_ROOT, 0o700)
    if _backend() == "local":
        target = _local_path(storage_key)
        temporary = ARTIFACT_ROOT / f".{storage_key}.{uuid4().hex}.tmp"
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    bucket, _, _, _ = _s3_config()
    _s3_client().upload_file(
        str(source),
        bucket,
        _object_key(storage_key),
        ExtraArgs={"ServerSideEncryption": os.getenv("PRAYOG_S3_SERVER_SIDE_ENCRYPTION", "AES256")},
    )
    return source


def resolve_artifact(storage_key: str) -> Path:
    _validate_key(storage_key)
    if _backend() == "local":
        return _local_path(storage_key)

    cached = _cache_path(storage_key)
    if cached.is_file():
        return cached
    ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ARTIFACT_ROOT, 0o700)
    temporary = ARTIFACT_ROOT / f".{storage_key}.{uuid4().hex}.download"
    bucket, _, _, _ = _s3_config()
    try:
        _s3_client().download_file(bucket, _object_key(storage_key), str(temporary))
        os.chmod(temporary, 0o600)
        os.replace(temporary, cached)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return cached


def delete_artifact(storage_key: str) -> None:
    _validate_key(storage_key)
    if _backend() == "local":
        _local_path(storage_key).unlink(missing_ok=True)
        return
    bucket, _, _, _ = _s3_config()
    _s3_client().delete_object(Bucket=bucket, Key=_object_key(storage_key))
    _cache_path(storage_key).unlink(missing_ok=True)
