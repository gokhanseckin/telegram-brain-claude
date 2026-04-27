"""Stage A: per-tag embedding centroids and nearest-tag lookup.

For each manually-tagged chat we average the most recent ~N message embeddings
to form a chat centroid. We then average chat centroids per tag to form a tag
centroid. To classify a new chat we average its recent embeddings and find
the nearest tag centroid by cosine similarity.

We compute everything in Python with NumPy. The embedding count is small
(~hundreds of chats x ~50 vectors of 1024 dims, a few MB total) so we don't push
the math into Postgres. pgvector is only used as the storage layer.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.orm import Session

log = structlog.get_logger(__name__)


@dataclass
class TagCentroid:
    tag: str
    vector: list[float]
    chat_count: int


@dataclass
class ClassificationResult:
    tag: str
    similarity: float
    margin: float  # gap between top-1 and top-2


def _normalize(v: list[float]) -> list[float]:
    n = sum(x * x for x in v) ** 0.5
    if n == 0:
        return v
    return [x / n for x in v]


def _avg(vectors: list[list[float]]) -> list[float] | None:
    if not vectors:
        return None
    dim = len(vectors[0])
    out = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            out[i] += x
    return [x / len(vectors) for x in out]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _fetch_chat_embeddings(
    session: Session, chat_id: int, sample_size: int
) -> list[list[float]]:
    """Pull the last `sample_size` embeddings for a chat."""
    rows = session.execute(
        text(
            """
            SELECT mu.embedding::text AS embedding
            FROM message_understanding mu
            JOIN messages m ON m.chat_id = mu.chat_id
                AND m.message_id = mu.message_id
            WHERE mu.chat_id = :chat_id
              AND mu.embedding IS NOT NULL
            ORDER BY m.sent_at DESC
            LIMIT :n
            """
        ),
        {"chat_id": chat_id, "n": sample_size},
    ).all()
    return [_parse_pgvector(row[0]) for row in rows]


def _parse_pgvector(s: str) -> list[float]:
    # pgvector serialises as "[0.1,0.2,...]"
    return [float(x) for x in s.strip("[]").split(",")]


def chat_centroid(
    session: Session, chat_id: int, sample_size: int
) -> tuple[list[float] | None, int]:
    """Return (centroid_vector, n_messages_used) for one chat."""
    vectors = _fetch_chat_embeddings(session, chat_id, sample_size)
    if not vectors:
        return None, 0
    return _avg(vectors), len(vectors)


def build_tag_centroids(
    session: Session, sample_size: int
) -> list[TagCentroid]:
    """Build one centroid per tag from chats with manual tags."""
    chats = session.execute(
        text(
            """
            SELECT chat_id, tag
            FROM chats
            WHERE tag IS NOT NULL
              AND tag != 'ignore'
              AND tag_source = 'manual'
            """
        )
    ).all()

    by_tag: dict[str, list[list[float]]] = {}
    for chat_id, tag in chats:
        cent, _n = chat_centroid(session, chat_id, sample_size)
        if cent is None:
            continue
        by_tag.setdefault(tag, []).append(cent)

    out: list[TagCentroid] = []
    for tag, vecs in by_tag.items():
        avg = _avg(vecs)
        if avg is not None:
            out.append(TagCentroid(tag=tag, vector=avg, chat_count=len(vecs)))
    log.info(
        "tag_centroids_built",
        tags={tc.tag: tc.chat_count for tc in out},
    )
    return out


def classify(
    chat_vector: list[float],
    tag_centroids: list[TagCentroid],
) -> ClassificationResult | None:
    """Return the nearest tag with similarity and margin to runner-up."""
    if not tag_centroids:
        return None
    sims = sorted(
        ((tc.tag, _cosine(chat_vector, tc.vector)) for tc in tag_centroids),
        key=lambda x: x[1],
        reverse=True,
    )
    top_tag, top_sim = sims[0]
    runner_sim = sims[1][1] if len(sims) > 1 else 0.0
    return ClassificationResult(
        tag=top_tag,
        similarity=top_sim,
        margin=top_sim - runner_sim,
    )
