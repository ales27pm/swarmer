"""Differential tests against the original validated, scaled cosine implementation."""

from __future__ import annotations

import math
import random
import sys

import pytest

from app.services.memory_vectors import (
    MAX_MEMORY_DIMENSIONS,
    memory_cosine,
    memory_prepared_cosine,
    memory_unit,
    memory_vector,
)


def reference_vector(value, dimensions=None):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MEMORY_DIMENSIONS:
        return None
    if dimensions is not None and len(value) != dimensions:
        return None
    if any(type(item) not in {int, float} for item in value):
        return None
    try:
        vector = [float(item) for item in value]
    except (ValueError, OverflowError):
        return None
    if not all(math.isfinite(item) for item in vector) or not any(vector):
        return None
    return vector


def reference_cosine(left, right):
    def unit(values):
        scale = max(abs(value) for value in values)
        scaled = [value / scale for value in values]
        norm = math.hypot(*scaled)
        return [value / norm for value in scaled]

    return max(
        -1.0, min(1.0, math.fsum(a * b for a, b in zip(unit(left), unit(right), strict=True)))
    )


class FloatSubclass(float):
    pass


class IntSubclass(int):
    pass


@pytest.mark.parametrize(
    "value",
    [
        None,
        (1, 2),
        [],
        [0, -0.0],
        [True, 1.0],
        [False, 1.0],
        [FloatSubclass(1.0)],
        [IntSubclass(1)],
        ["1", 2],
        [[1], 2],
        [None, 1],
        [math.nan, 1],
        [math.inf, 1],
        [-math.inf, 1],
        [10**1000, 1],
        [1, -2.5, 0],
        [5e-324, -5e-324],
        [sys.float_info.max, -sys.float_info.max],
        [1.0] * MAX_MEMORY_DIMENSIONS,
        [1.0] * (MAX_MEMORY_DIMENSIONS + 1),
    ],
    ids=lambda value: type(value).__name__,
)
def test_validation_preserves_original_type_dimension_and_finiteness_contract(value):
    for dimensions in (None, 1, 2, 3, MAX_MEMORY_DIMENSIONS):
        assert memory_vector(value, dimensions) == reference_vector(value, dimensions)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ([1.0], [1.0]),
        ([1.0], [-1.0]),
        ([1.0, 0.0], [0.0, 1.0]),
        ([0.3, 0.7], [-0.7, 0.3]),
        ([5e-324, 5e-324], [5e-324, 0.0]),
        ([5e-324, -5e-324], [-5e-324, 5e-324]),
        ([1e-310, 2e-310, -3e-310], [4e-310, -5e-310, 6e-310]),
        ([sys.float_info.min, 5e-324], [5e-324, sys.float_info.min]),
        ([sys.float_info.max] * 3, [sys.float_info.max, -sys.float_info.max, 0.0]),
        ([1e308, 1e308, -1e308], [1e308, -1e308, 1e308]),
        ([1e308, 5e-324, -1e308], [5e-324, 1e308, 0.0]),
        ([1.0] * MAX_MEMORY_DIMENSIONS, [-1.0] * MAX_MEMORY_DIMENSIONS),
    ],
)
def test_prepared_cosine_preserves_robust_extreme_math(left, right):
    prepared = memory_unit(left)
    expected = reference_cosine(left, right)
    for result in (memory_cosine(left, right), memory_prepared_cosine(prepared, right)):
        assert math.isfinite(result) and -1 <= result <= 1
        assert result == pytest.approx(expected, rel=0, abs=5e-16)
    assert math.hypot(*prepared) == pytest.approx(1.0, rel=0, abs=3e-16)


def test_seeded_vectors_across_dimensions_and_finite_exponents_match_original():
    rng = random.Random(20261003)
    for dimensions in (1, 2, 3, 16, 128, 768):
        for _ in range(100):
            left = [
                rng.uniform(-1.0, 1.0) * 10 ** rng.randint(-320, 308) for _ in range(dimensions)
            ]
            right = [
                rng.uniform(-1.0, 1.0) * 10 ** rng.randint(-320, 308) for _ in range(dimensions)
            ]
            assert memory_vector(left) == reference_vector(left) == left
            assert memory_vector(right) == reference_vector(right) == right
            assert memory_cosine(left, right) == pytest.approx(
                reference_cosine(left, right), rel=0, abs=5e-16
            )


def test_prepared_query_top_50_preserves_ranks_and_tie_order():
    rng = random.Random(731)
    for dimensions in (2, 16, 768):
        query = [rng.uniform(-1.0, 1.0) for _ in range(dimensions)]
        prepared = memory_unit(query)
        vectors = [[rng.uniform(-1.0, 1.0) for _ in range(dimensions)] for _ in range(300)]
        # Exact duplicates exercise the pinned/id tie breakers used by the reader.
        vectors.extend([vectors[0], vectors[0], query, query])
        old_scores = [reference_cosine(query, vector) for vector in vectors]
        new_scores = [memory_prepared_cosine(prepared, vector) for vector in vectors]

        def ranked(scores):
            return sorted(
                (i for i, score in enumerate(scores) if score > 0),
                key=lambda i: (-scores[i], i % 2 == 0, f"memory_{i:03}"),
            )[:50]

        assert ranked(new_scores) == ranked(old_scores)
        assert new_scores == pytest.approx(old_scores, rel=0, abs=5e-16)


def test_prepared_query_and_document_vectors_are_not_mutated():
    left, right = [1e308, -1e308], [5e-324, 5e-324]
    prepared = memory_unit(left)
    snapshot = prepared.copy()
    assert memory_prepared_cosine(prepared, right) == 0.0
    assert prepared == snapshot
    assert left == [1e308, -1e308] and right == [5e-324, 5e-324]


def test_dimension_mismatch_is_not_silently_truncated():
    with pytest.raises(ValueError):
        memory_prepared_cosine(memory_unit([1.0, 0.0]), [1.0])
    with pytest.raises(ValueError):
        memory_cosine([1.0], [1.0, 0.0])
