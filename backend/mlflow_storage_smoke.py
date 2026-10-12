import hashlib
import os
from pathlib import Path
from uuid import uuid4

from logic import artifact_storage


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main():
    backend = artifact_storage._backend()
    if backend == "s3":
        artifact_storage._s3_config()
    artifact_storage.ARTIFACT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(artifact_storage.ARTIFACT_ROOT, 0o700)

    storage_key = uuid4().hex
    source = artifact_storage.ARTIFACT_ROOT / f".storage-smoke-{uuid4().hex}.tmp"
    expected = b"Prayog artifact storage smoke test\n"
    source.write_bytes(expected)
    os.chmod(source, 0o600)

    try:
        artifact_storage.persist_artifact(source, storage_key)
        resolved = artifact_storage.resolve_artifact(storage_key)
        if _digest(resolved) != hashlib.sha256(expected).hexdigest():
            raise RuntimeError("Artifact storage smoke test failed checksum verification.")
        artifact_storage.delete_artifact(storage_key)
        if backend == "local":
            if artifact_storage._local_path(storage_key).exists():
                raise RuntimeError("Artifact storage smoke test failed cleanup verification.")
        elif artifact_storage._cache_path(storage_key).exists():
            raise RuntimeError("Artifact storage smoke test failed cache cleanup verification.")
    except Exception:
        try:
            artifact_storage.delete_artifact(storage_key)
        except Exception:
            pass
        raise
    finally:
        source.unlink(missing_ok=True)

    print(f"Artifact storage smoke test passed for backend: {backend}")


if __name__ == "__main__":
    main()
