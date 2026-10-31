"""Focused tests for the predeclared campaign optimization target."""

from __future__ import annotations

import pytest

from measure_methods.observations import confirmed_acceptance_lower_bound


@pytest.mark.parametrize(
    ("acceptance", "expected"),
    [("accepted", 1.0), ("not_accepted", 0.0), ("unknown", 0.0)],
)
def test_confirmed_acceptance_lower_bound_is_conservative(acceptance, expected):
    assert confirmed_acceptance_lower_bound(acceptance) == expected


@pytest.mark.parametrize("acceptance", ["missing", "failed", "Accepted", "", None, 0])
def test_confirmed_acceptance_lower_bound_rejects_states_outside_contract(acceptance):
    with pytest.raises((TypeError, ValueError)):
        confirmed_acceptance_lower_bound(acceptance)
