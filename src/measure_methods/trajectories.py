"""Pure summaries of campaign success over high-fidelity evaluation budgets."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Real
from typing import Literal


@dataclass(frozen=True)
class SuccessObservation:
    """One ordered campaign outcome and its declared resource inputs.

    ``program_volume_ml`` is the volume attributed to this observation. The
    trajectory summary uses volumes from high-fidelity observations only.
    """

    sequence: int
    fidelity_role: Literal["high", "low"]
    satisfied: bool | None
    program_volume_ml: Real | None = None


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite nonnegative real number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite nonnegative real number") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite nonnegative real number")
    return result


def _observations(value: object) -> Sequence[SuccessObservation]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("observations must be a sequence of SuccessObservation values")
    if not value:
        raise ValueError("observations must contain at least one campaign run")
    for item in value:
        if not isinstance(item, SuccessObservation):
            raise ValueError("observations must contain only SuccessObservation values")
    return value


def _validated_observation(
    item: SuccessObservation, expected_sequence: int
) -> tuple[str, bool | None, float | None]:
    sequence = _positive_integer(item.sequence, "sequence")
    if sequence != expected_sequence:
        raise ValueError("observation sequences must be contiguous and start at one")
    if item.fidelity_role not in ("high", "low"):
        raise ValueError("fidelity_role must be 'high' or 'low'")
    if item.satisfied is not None and type(item.satisfied) is not bool:
        raise ValueError("satisfied must be a boolean or null")
    volume = (
        None
        if item.program_volume_ml is None
        else _nonnegative_real(item.program_volume_ml, "program_volume_ml")
    )
    return item.fidelity_role, item.satisfied, volume


def summarize_campaign_success(
    observations: Sequence[SuccessObservation],
    *,
    budget: int,
    low_fidelity_cost: Real | None = None,
) -> dict:
    """Summarize first confirmed high-fidelity success and budget states.

    High-fidelity indices count only high-fidelity observations, including any
    initial-design runs. A low-fidelity outcome never confirms success. An
    unavailable high-fidelity outcome leaves that index unknown. Missing runs
    after the observed history are also unknown within ``budget``.

    The evaluation-cost proxy is ``high_fidelity_count + low_fidelity_count *
    low_fidelity_cost`` through the first confirmed high-fidelity success. If
    low-fidelity runs precede success and their declared cost is unavailable,
    the proxy is null. Program volume sums high-fidelity rows through that same
    success; any missing high-fidelity volume in that prefix makes the sum
    unknown. Low-fidelity volumes are validated but excluded.
    """
    budget = _positive_integer(budget, "budget")
    declared_low_cost = (
        None
        if low_fidelity_cost is None
        else _nonnegative_real(low_fidelity_cost, "low_fidelity_cost")
    )
    rows = _observations(observations)

    high_count = 0
    low_count = 0
    unavailable_indices: list[int] = []
    high_volume_total = 0.0
    high_volume_missing = False
    first_confirmed: dict | None = None

    for expected_sequence, item in enumerate(rows, 1):
        role, satisfied, volume = _validated_observation(item, expected_sequence)
        if role == "low":
            low_count += 1
            continue

        high_count += 1
        if satisfied is None:
            unavailable_indices.append(high_count)
        if first_confirmed is not None:
            continue

        if volume is None:
            high_volume_missing = True
        else:
            try:
                high_volume_total = math.fsum((high_volume_total, volume))
            except OverflowError as error:
                raise ValueError(
                    "cumulative high-fidelity program volume must be finite"
                ) from error
            if not math.isfinite(high_volume_total):
                raise ValueError("cumulative high-fidelity program volume must be finite")

        if satisfied is True:
            if declared_low_cost is None and low_count:
                cost_proxy = None
                cost_status = "unknown_declared_low_fidelity_cost_missing"
            else:
                low_cost = 0.0 if declared_low_cost is None else declared_low_cost
                cost_proxy = float(high_count) + float(low_count) * low_cost
                if not math.isfinite(cost_proxy):
                    raise ValueError("first-success evaluation cost proxy must be finite")
                cost_status = "known"

            if high_volume_missing:
                program_volume = None
                volume_status = "unknown_high_fidelity_program_volume_missing"
            else:
                program_volume = high_volume_total
                volume_status = "known"

            first_confirmed = {
                "high_fidelity_index": high_count,
                "sequence": expected_sequence,
                "high_fidelity_evaluation_count": high_count,
                "low_fidelity_evaluation_count": low_count,
                "evaluation_cost_proxy": cost_proxy,
                "evaluation_cost_proxy_status": cost_status,
                "high_fidelity_program_volume_ml": program_volume,
                "program_volume_status": volume_status,
            }

    if first_confirmed is None:
        first_possible = unavailable_indices[0] if unavailable_indices else high_count + 1
    else:
        possible_before_confirmation = [
            index
            for index in unavailable_indices
            if index <= first_confirmed["high_fidelity_index"]
        ]
        first_possible = min(
            [first_confirmed["high_fidelity_index"], *possible_before_confirmation]
        )
        first_confirmed["lower_bound_high_fidelity_index"] = first_possible
        first_confirmed["upper_bound_high_fidelity_index"] = first_confirmed["high_fidelity_index"]

    trajectory = []
    for spent in range(1, budget + 1):
        if first_confirmed is not None and first_confirmed["high_fidelity_index"] <= spent:
            success_state = "confirmed_success"
        elif any(index <= spent for index in unavailable_indices) or high_count < spent:
            success_state = "unknown"
        else:
            success_state = "observed_no_success"
        trajectory.append(
            {
                "high_fidelity_budget": spent,
                "success_state": success_state,
                "observed_depth": min(high_count, spent),
                "unknown": success_state == "unknown",
            }
        )

    return {
        "budget": budget,
        "low_fidelity_cost": declared_low_cost,
        "observation_count": len(rows),
        "high_fidelity_observation_count": high_count,
        "low_fidelity_observation_count": low_count,
        "unavailable_high_fidelity_indices": unavailable_indices,
        "first_confirmed_success": first_confirmed,
        "first_possible_success_high_fidelity_index": first_possible,
        "success_within_budget": (
            first_confirmed is not None and first_confirmed["high_fidelity_index"] <= budget
        ),
        "trajectory": trajectory,
    }


def _summary_integer(summary: dict, key: str, *, minimum: int = 0) -> int:
    value = summary.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"campaign summary {key} must be an integer of at least {minimum}")
    return value


def aggregate_success_trajectories(
    campaigns: Sequence[dict], *, planned_count: int, budget: int
) -> list[dict]:
    """Aggregate campaign summaries using the full planned cohort denominator.

    ``campaigns`` contains summaries returned by
    :func:`summarize_campaign_success`. Campaigns not present in this sequence
    are retained in the denominator and counted as unknown at every budget.
    """
    planned_count = _positive_integer(planned_count, "planned_count")
    budget = _positive_integer(budget, "budget")
    if isinstance(campaigns, (str, bytes)) or not isinstance(campaigns, Sequence):
        raise ValueError("campaigns must be a sequence of campaign summaries")
    if len(campaigns) > planned_count:
        raise ValueError("recorded campaign count cannot exceed planned_count")

    validated: list[tuple[int, list[dict]]] = []
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            raise ValueError("campaigns must contain summary dictionaries")
        if _summary_integer(campaign, "budget", minimum=1) != budget:
            raise ValueError("campaign summary budget must match aggregate budget")
        high_count = _summary_integer(campaign, "high_fidelity_observation_count")
        first_success = campaign.get("first_confirmed_success")
        if first_success is None:
            first_success_index = None
        elif isinstance(first_success, dict):
            first_success_index = _summary_integer(first_success, "high_fidelity_index", minimum=1)
            if first_success_index > high_count:
                raise ValueError("first confirmed success cannot exceed observed HF depth")
        else:
            raise ValueError("first_confirmed_success must be a summary dictionary or null")

        unavailable = campaign.get("unavailable_high_fidelity_indices")
        if isinstance(unavailable, (str, bytes)) or not isinstance(unavailable, Sequence):
            raise ValueError("unavailable HF indices must be a sequence")
        unavailable_indices = []
        for index in unavailable:
            index = _positive_integer(index, "unavailable high-fidelity index")
            if index > high_count or (unavailable_indices and index <= unavailable_indices[-1]):
                raise ValueError("unavailable HF indices must be ordered within observed depth")
            unavailable_indices.append(index)

        trajectory = campaign.get("trajectory")
        if (
            isinstance(trajectory, (str, bytes))
            or not isinstance(trajectory, Sequence)
            or len(trajectory) != budget
        ):
            raise ValueError("campaign summary trajectory must cover every budget point")
        points: list[dict] = []
        for spent, point in enumerate(trajectory, 1):
            if not isinstance(point, dict):
                raise ValueError("campaign trajectory points must be dictionaries")
            if (
                type(point.get("high_fidelity_budget")) is not int
                or point["high_fidelity_budget"] != spent
            ):
                raise ValueError("campaign trajectory budgets must be contiguous and start at one")
            if type(point.get("observed_depth")) is not int or point["observed_depth"] != min(
                high_count, spent
            ):
                raise ValueError("campaign trajectory observed_depth is inconsistent")
            if first_success_index is not None and first_success_index <= spent:
                expected_state = "confirmed_success"
            elif any(index <= spent for index in unavailable_indices) or high_count < spent:
                expected_state = "unknown"
            else:
                expected_state = "observed_no_success"
            if point.get("success_state") != expected_state:
                raise ValueError("campaign trajectory success_state is inconsistent")
            if type(point.get("unknown")) is not bool or point["unknown"] != (
                expected_state == "unknown"
            ):
                raise ValueError("campaign trajectory unknown flag is inconsistent")
            points.append(point)
        validated.append((high_count, points))

    missing_count = planned_count - len(validated)
    aggregate = []
    for spent in range(1, budget + 1):
        success_count = sum(
            points[spent - 1]["success_state"] == "confirmed_success" for _, points in validated
        )
        campaign_unknown_count = sum(
            points[spent - 1]["success_state"] == "unknown" for _, points in validated
        )
        unknown_count = missing_count + campaign_unknown_count
        observed_no_success_count = planned_count - success_count - unknown_count
        observed_count = sum(high_count >= spent for high_count, _ in validated)
        aggregate.append(
            {
                "high_fidelity_budget": spent,
                "cohort_size": planned_count,
                "recorded_campaign_count": len(validated),
                "observed_at_budget_count": observed_count,
                "success_count": success_count,
                "unknown_success_count": unknown_count,
                "observed_no_success_count": observed_no_success_count,
                "success_fraction": success_count / planned_count,
                "success_fraction_upper_bound": (success_count + unknown_count) / planned_count,
            }
        )
    return aggregate
