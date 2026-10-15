import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from blake3 import blake3

from logic.errors import BadRequestError

ARTIFACT_ROOT = Path(
    os.getenv("PRAYOG_ARTIFACT_ROOT", Path(__file__).resolve().parents[1] / "artifacts")
).resolve()
_UUID_KEY = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_CONTENT_KEY = re.compile(r"[0-9a-f]{64}\Z")


def _validate_key(storage_key: str) -> str:
    if not storage_key or not (
        _UUID_KEY.fullmatch(storage_key.lower()) or _CONTENT_KEY.fullmatch(storage_key.lower())
    ):
        raise BadRequestError("Invalid artifact storage key")
    return storage_key.lower()


def _is_content_key(storage_key: str) -> bool:
    return bool(_CONTENT_KEY.fullmatch(_validate_key(storage_key)))


def _file_blake3(path: Path) -> str:
    digest = blake3()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_content_address(path: Path, storage_key: str) -> None:
    if _is_content_key(storage_key) and _file_blake3(path) != storage_key:
        raise BadRequestError("Content-addressed artifact integrity verification failed")


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


def _existing_s3_object_is_valid(client, bucket: str, object_key: str, storage_key: str, source: Path) -> bool:
    from botocore.exceptions import ClientError

    try:
        head = client.head_object(Bucket=bucket, Key=object_key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise

    expected_size = source.stat().st_size
    if head.get("ContentLength") != expected_size:
        raise RuntimeError("Existing content-addressed S3 object has an unexpected size")
    recorded_digest = (head.get("Metadata") or {}).get("blake3")
    if recorded_digest and recorded_digest.lower() != storage_key:
        raise RuntimeError("Existing S3 object metadata conflicts with its content address")
    # Metadata is only a hint: it can survive an out-of-band object overwrite.
    # Verify the actual bytes even when the object advertises the expected digest.
    temporary = ARTIFACT_ROOT / f".{uuid4().hex}.verify"
    try:
        client.download_file(bucket, object_key, str(temporary))
        os.chmod(temporary, 0o600)
        if _file_blake3(temporary) != storage_key:
            raise RuntimeError("Existing S3 object does not match its content address")
    finally:
        temporary.unlink(missing_ok=True)
    return True


def persist_artifact(source: Path, storage_key: str) -> Path:
    key = _validate_key(storage_key)
    if _is_content_key(key):
        _verify_content_address(source, key)
    ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ARTIFACT_ROOT, 0o700)
    if _backend() == "local":
        target = _local_path(key)
        temporary = ARTIFACT_ROOT / f".{uuid4().hex}.tmp"
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            if _is_content_key(key):
                try:
                    # A hard link makes first-writer-wins atomic without overwriting
                    # an existing immutable content object.
                    os.link(temporary, target)
                except FileExistsError:
                    _verify_content_address(target, key)
            else:
                os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    bucket, _, _, _ = _s3_config()
    client = _s3_client()
    object_key = _object_key(key)
    if _is_content_key(key) and _existing_s3_object_is_valid(client, bucket, object_key, key, source):
        return source
    extra_args = {
        "ServerSideEncryption": os.getenv("PRAYOG_S3_SERVER_SIDE_ENCRYPTION", "AES256"),
    }
    if _is_content_key(key):
        extra_args["Metadata"] = {"blake3": key}
    client.upload_file(str(source), bucket, object_key, ExtraArgs=extra_args)
    return source


def resolve_artifact(storage_key: str) -> Path:
    key = _validate_key(storage_key)
    if _backend() == "local":
        path = _local_path(key)
        if path.is_file():
            _verify_content_address(path, key)
        return path

    cached = _cache_path(key)
    if cached.is_file():
        try:
            _verify_content_address(cached, key)
            return cached
        except BadRequestError:
            cached.unlink(missing_ok=True)
    ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ARTIFACT_ROOT, 0o700)
    temporary = ARTIFACT_ROOT / f".{uuid4().hex}.download"
    bucket, _, _, _ = _s3_config()
    try:
        _s3_client().download_file(bucket, _object_key(key), str(temporary))
        os.chmod(temporary, 0o600)
        _verify_content_address(temporary, key)
        os.replace(temporary, cached)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return cached


def delete_artifact(storage_key: str) -> None:
    key = _validate_key(storage_key)
    if _is_content_key(key):
        raise BadRequestError(
            "Content-addressed objects are immutable; use reference-aware garbage collection"
        )
    if _backend() == "local":
        _local_path(key).unlink(missing_ok=True)
        return
    bucket, _, _, _ = _s3_config()
    _s3_client().delete_object(Bucket=bucket, Key=_object_key(key))
    _cache_path(key).unlink(missing_ok=True)
