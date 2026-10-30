"""Seeded experimental designs with explicit bounds and binary64 semantics.

Coordinates have the units of the supplied bounds. Seeds are numerical stream
inputs: this kernel has no knowledge of protocol identities, ownership, storage,
method feasibility, service schemas or optimizer state.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from numbers import Real


def _inputs(bounds, dimension_seeds):
    if (
        isinstance(bounds, (str, bytes))
        or not isinstance(bounds, Sequence)
        or not 1 <= len(bounds) <= 64
        or isinstance(dimension_seeds, (str, bytes))
        or not isinstance(dimension_seeds, Sequence)
        or len(bounds) != len(dimension_seeds)
    ):
        raise ValueError("Supply 1 to 64 bound pairs and exactly one integer seed per dimension")
    normalized = []
    for pair, seed in zip(bounds, dimension_seeds, strict=True):
        if isinstance(pair, (str, bytes)) or not isinstance(pair, Sequence) or len(pair) != 2:
            raise ValueError("Each bound must be an explicit lower/upper pair")
        if any(isinstance(value, bool) or not isinstance(value, Real) for value in pair):
            raise ValueError("Bounds must be finite real numbers, excluding booleans")
        try:
            lower, upper = map(float, pair)
        except (ValueError, OverflowError) as error:
            raise ValueError("Bounds must be representable as binary64") from error
        if (
            not all(math.isfinite(value) for value in (lower, upper, upper - lower))
            or lower >= upper
        ):
            raise ValueError("Bounds must have a finite positive binary64 span")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("Each dimension seed must be a nonnegative integer")
        normalized.append((lower, upper))
    return normalized


def latin_hypercube_design(
    *, bounds: Sequence[Sequence[Real]], size: int, dimension_seeds: Sequence[int]
) -> tuple[tuple[float, ...], ...]:
    """Generate one independently permuted and jittered coordinate per stratum.

    The returned rows are design points. Every dimension uses its own
    ``random.Random`` stream. Bounds are closed after binary64 scaling; the
    final stratum includes its upper endpoint. Reject repeated points or a
    scaled coordinate that leaves its intended stratum instead of advertising
    a Latin hypercube that cannot be realized at the supplied precision.
    """
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 4096:
        raise ValueError("size must be an integer from 1 through 4096")
    normalized = _inputs(bounds, dimension_seeds)
    dimensions = []
    for (lower, upper), seed in zip(normalized, dimension_seeds, strict=True):
        rng = random.Random(seed)
        strata = list(range(size))
        rng.shuffle(strata)
        jitters = [rng.random() for _ in range(size)]
        units = [(strata[index] + jitters[index]) / size for index in range(size)]
        units = [math.nextafter(1.0, 0.0) if unit >= 1.0 else unit for unit in units]
        values = [lower + (upper - lower) * unit for unit in units]
        realized = set()
        for unit, value in zip(units, values, strict=True):
            actual_unit = (value - lower) / (upper - lower)
            intended_stratum = min(size - 1, int(unit * size))
            if not math.isfinite(actual_unit) or not 0.0 <= actual_unit <= 1.0:
                raise ValueError("Bounds cannot realize the initial LHS at binary64 precision")
            actual_stratum = min(size - 1, int(actual_unit * size))
            if actual_stratum != intended_stratum:
                raise ValueError("Bounds cannot realize every intended initial LHS stratum")
            realized.add(actual_stratum)
        if realized != set(range(size)):
            raise ValueError("Bounds cannot realize every unique initial LHS stratum")
        dimensions.append(values)
    points = tuple(tuple(point) for point in zip(*dimensions, strict=True))
    if len(set(points)) != size:
        raise ValueError("Bounds cannot realize unique initial LHS points")
    return points


def uniform_random_point(
    *, bounds: Sequence[Sequence[Real]], dimension_seeds: Sequence[int]
) -> tuple[float, ...]:
    """Draw one independent uniform coordinate per explicit dimension stream."""
    normalized = _inputs(bounds, dimension_seeds)
    return tuple(
        lower + (upper - lower) * random.Random(seed).random()
        for (lower, upper), seed in zip(normalized, dimension_seeds, strict=True)
    )
