"""Reusable seeded designs stay independent of service identity and storage."""

import math
import subprocess
import sys

import pytest

from measure_methods.design import latin_hypercube_design, uniform_random_point


def test_explicit_two_stream_reference_and_coordinate_scaling():
    points = latin_hypercube_design(bounds=[(0, 1), (2, 10)], size=3, dimension_seeds=[7, 13])
    expected = (
        (0.7982744988077245, 3.827354647905432),
        (0.016095474542270782, 9.157551781376295),
        (0.6070914306637695, 6.931563097039814),
    )
    assert points == expected
    for coordinate, (lower, upper) in enumerate(((0, 1), (2, 10))):
        assert {int(3 * (point[coordinate] - lower) / (upper - lower)) for point in points} == {
            0,
            1,
            2,
        }
    assert points == latin_hypercube_design(
        bounds=[(0, 1), (2, 10)], size=3, dimension_seeds=[7, 13]
    )


def test_uniform_point_is_replayable_with_independent_dimension_streams():
    first = uniform_random_point(bounds=[(-2, 4), (5, 10)], dimension_seeds=[17, 29])
    assert -2 <= first[0] <= 4 and 5 <= first[1] <= 10
    assert first == uniform_random_point(bounds=[(-2, 4), (5, 10)], dimension_seeds=[17, 29])
    assert first != uniform_random_point(bounds=[(-2, 4), (5, 10)], dimension_seeds=[18, 30])


@pytest.mark.parametrize(
    "bounds,seeds",
    [
        ([], []),
        ([(0, 1)], []),
        ([(0, 1)], [True]),
        ([(0, 1)], [-1]),
        ([(False, 1)], [1]),
        ([(0, math.nan)], [1]),
        ([(0, math.inf)], [1]),
        ([(0, 0)], [1]),
        ([(1, 0)], [1]),
        ([(-1e308, 1e308)], [1]),
        (["01"], [1]),
        ([(0, 1, 2)], [1]),
        ([(0, "1")], [1]),
    ],
)
def test_invalid_numerical_design_inputs_fail_in_both_methods(bounds, seeds):
    with pytest.raises(ValueError):
        latin_hypercube_design(bounds=bounds, size=3, dimension_seeds=seeds)
    with pytest.raises(ValueError):
        uniform_random_point(bounds=bounds, dimension_seeds=seeds)


@pytest.mark.parametrize("size", [True, 0, -1, 4097, 3.0, "3"])
def test_design_size_is_a_bounded_integer(size):
    with pytest.raises(ValueError):
        latin_hypercube_design(bounds=[(0, 1)], size=size, dimension_seeds=[17])


def test_latin_hypercube_rejects_unrealizable_binary64_strata():
    with pytest.raises(ValueError, match="LHS"):
        latin_hypercube_design(
            bounds=[(0.5, math.nextafter(0.5, 1.0))], size=3, dimension_seeds=[1]
        )


def test_design_import_requires_neither_service_nor_schema_or_optimizer(tmp_path):
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from measure_methods.design import latin_hypercube_design, uniform_random_point
assert len(latin_hypercube_design(bounds=[(0, 1)], size=3, dimension_seeds=[17])) == 3
assert len(uniform_random_point(bounds=[(0, 1)], dimension_seeds=[17])) == 1
assert not any(name in sys.modules for name in ('measure_service', 'mcp', 'pydantic', 'bo_engine'))
""",
        ],
        cwd=tmp_path,
        check=True,
    )
