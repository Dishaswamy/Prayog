import math
import re
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from logic.errors import BadRequestError
from mlflow_models import RebuildAttempt, StewardContextChunk

SOURCE_TYPES = {"DIAGNOSTIC_LOG", "MLFLOW_METADATA", "EXTERNAL_DOC"}
MAX_CHUNK_CHARS = 20000
MAX_EMBEDDING_DIMENSIONS = 4096


def _valid_embedding(value):
    if value is None:
        return None
    if not isinstance(value, list) or not value or len(value) > MAX_EMBEDDING_DIMENSIONS:
        raise BadRequestError("Embedding must be a non-empty numeric vector of supported size")
    try:
        vector = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise BadRequestError("Embedding must contain only finite numbers") from exc
    if not all(math.isfinite(item) for item in vector):
        raise BadRequestError("Embedding must contain only finite numbers")
    return vector


def store_context_chunk(
    db: Session,
    user_id: int | None,
    attempt_id: int | None,
    source_type: str,
    content: str,
    embedding: list[float] | None = None,
    *,
    sanitized: bool = False,
) -> StewardContextChunk:
    if not user_id or not attempt_id:
        raise BadRequestError("A user and rebuild attempt scope are required")
    if not sanitized:
        raise BadRequestError("Only sanitized context may be indexed")
    if source_type not in SOURCE_TYPES:
        raise BadRequestError("Unsupported Steward context source type")
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_CHUNK_CHARS:
        raise BadRequestError("Context content must be non-empty and within the size limit")
    attempt = db.query(RebuildAttempt).filter(
        RebuildAttempt.id == attempt_id,
        RebuildAttempt.user_id == user_id,
        RebuildAttempt.state == "REBUILD_FAILED",
    ).first()
    if attempt is None:
        raise BadRequestError("Context cannot be indexed for this attempt")
    vector = _valid_embedding(embedding)
    chunk = StewardContextChunk(
        id=str(uuid4()),
        user_id=user_id,
        rebuild_id=attempt_id,
        source_type=source_type,
        content=content.strip(),
        embedding=vector,
    )
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return chunk


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _lexical_score(content: str, query: str) -> float:
    terms = set(re.findall(r"[a-z0-9_]+", query.lower()))
    if not terms:
        return 0.0
    words = set(re.findall(r"[a-z0-9_]+", content.lower()))
    return len(terms & words) / len(terms)


def retrieve_context(
    db: Session,
    user_id: int | None,
    attempt_id: int | None,
    query: str = "",
    query_embedding: list[float] | None = None,
    top_k: int = 5,
) -> list[dict]:
    # Default-deny before any database query if either scope component is absent.
    if not user_id or not attempt_id or not isinstance(top_k, int) or top_k < 1 or top_k > 20:
        return []
    attempt = db.query(RebuildAttempt).filter(
        RebuildAttempt.id == attempt_id,
        RebuildAttempt.user_id == user_id,
        RebuildAttempt.state == "REBUILD_FAILED",
    ).first()
    if attempt is None:
        return []

    vector = _valid_embedding(query_embedding)
    # Apply both ownership and attempt partitioning in SQL before content is loaded.
    chunks = db.query(StewardContextChunk).filter(
        StewardContextChunk.user_id == user_id,
        StewardContextChunk.rebuild_id == attempt_id,
    ).order_by(StewardContextChunk.created_at.desc()).limit(100).all()
    ranked = []
    for chunk in chunks:
        if vector is not None and chunk.embedding is not None:
            score = _cosine_similarity(vector, chunk.embedding)
        else:
            score = _lexical_score(chunk.content, query)
        ranked.append((score, chunk))
    ranked.sort(key=lambda item: (item[0], item[1].created_at or datetime.min), reverse=True)
    return [
        {
            "chunk_id": chunk.id,
            "attempt_id": chunk.rebuild_id,
            "source_type": chunk.source_type,
            "content": chunk.content,
            "score": score,
        }
        for score, chunk in ranked[:top_k]
    ]
