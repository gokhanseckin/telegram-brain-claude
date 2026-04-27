"""Centroid math is pure Python; we test it without a DB."""

from __future__ import annotations

from tbc_worker_chat_tagger.centroids import (
    TagCentroid,
    _avg,
    _cosine,
    classify,
)


def test_avg_basic():
    assert _avg([[1.0, 2.0], [3.0, 4.0]]) == [2.0, 3.0]


def test_cosine_orthogonal():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_cosine_identical():
    assert abs(_cosine([1.0, 2.0], [1.0, 2.0]) - 1.0) < 1e-9


def test_classify_picks_nearest():
    centroids = [
        TagCentroid(tag="client", vector=[1.0, 0.0], chat_count=2),
        TagCentroid(tag="friend", vector=[0.0, 1.0], chat_count=2),
    ]
    result = classify([0.99, 0.01], centroids)
    assert result is not None
    assert result.tag == "client"
    assert result.similarity > 0.9
    assert result.margin > 0.0


def test_classify_empty():
    assert classify([1.0, 0.0], []) is None
