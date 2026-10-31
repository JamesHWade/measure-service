"""Strict protocol and deterministic candidates for paired synthetic LC campaigns.

The profile describes an isocratic two-solvent method with solvent B as the
strong-solvent fraction. It declares a bounded experiment; it does not execute
an optimizer, assess analytical acceptance, fit retention models, or qualify a
physical instrument.
"""

from __future__ import annotations

import hashlib
import math
from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from measure_methods.design import latin_hypercube_design, uniform_random_point

from .methods import (
    FiniteNumber,
    Identifier,
    MethodConstraints,
    MethodDeclaration,
    StrictModel,
    evaluate_method,
)
from .storage import canonical, sha256

CAMPAIGN_PROFILE = "synthetic-isocratic-paired/1"
ABLATION_CAMPAIGN_PROFILE = "synthetic-isocratic-ablation/1"
UTILITY_PROFILE = "moses-botier-1.0.0-binary64/1"
UTILITY_INPUT_DEFINITION = "finite-resolution-reference/1"
CONFIRMED_ACCEPTANCE_PROFILE = "confirmed-acceptance-lower-bound/1"
ANALYTICAL_SUCCESS_POLICY = "separate-strict-observation-contract"
RANDOM_POLICY_ID = "random"
BO_POLICY_ID = "bo_mcp_gp_logei"
SUPPORTED_POLICY_IDS = (RANDOM_POLICY_ID, BO_POLICY_ID)
SUPPORTED_OPTIMIZER_TARGETS = ("historical_utility", "confirmed_acceptance_reward")
SUPPORTED_SURROGATE_NOISE_PRIORS = ("default", "low_noise")
INITIAL_DESIGN_POLICY = "common_coordinates_counted_per_policy_seed"
STOPPING_POLICY = "full_budget"
FIDELITY_VALUE = 1.0
FIDELITY_ROLE = "high"
OBSERVATION_ORIGIN = "simulator_latent"
SOLVENT_IDS = ("A", "B")
_MAX_SEEDS = 20
_MAX_TASKS = 10
_MAX_EVALUATION_BUDGET = 50
_MAX_SEED_VALUE = 2**31 - 1

PositiveNumber = Annotated[FiniteNumber, Field(gt=0)]
PolicyId = Literal["random", "bo_mcp_gp_logei"]
SeedValue = Annotated[StrictInt, Field(ge=0, le=_MAX_SEED_VALUE)]
_SeedList = Annotated[list[SeedValue], Field(min_length=1, max_length=_MAX_SEEDS)]


class DecisionBounds(StrictModel):
    """Closed numeric bounds for one dimension of the candidate space."""

    lower: FiniteNumber
    upper: FiniteNumber


class CampaignDomain(StrictModel):
    """Strong-solvent fraction and acquisition-duration limits."""

    strong_solvent_fraction: DecisionBounds = Field(
        description="Closed unitless fraction bounds for solvent B; solvent A is 1 minus B."
    )
    acquisition_duration_min: DecisionBounds = Field(
        description="Closed acquisition-duration bounds in minutes, with a minimum of one minute."
    )

    @model_validator(mode="after")
    def validate_bounds(self) -> CampaignDomain:
        strong = self.strong_solvent_fraction
        if not 0.0 <= strong.lower < strong.upper <= 1.0:
            raise ValueError("strong_solvent_fraction bounds must satisfy 0 <= lower < upper <= 1")
        duration = self.acquisition_duration_min
        if not 1.0 <= duration.lower < duration.upper:
            raise ValueError("acquisition_duration_min bounds must satisfy 1 <= lower < upper")
        return self


class CampaignTaskLink(StrictModel):
    """Caller-named producer and observation-contract links for one task."""

    task_id: Identifier
    producer_id: Identifier
    observation_contract_id: Identifier


_TaskList = Annotated[list[CampaignTaskLink], Field(min_length=1, max_length=_MAX_TASKS)]


class CampaignProtocolSpec(StrictModel):
    """Frozen-shape declaration for one bounded paired synthetic campaign."""

    profile: Literal["synthetic-isocratic-paired/1"] = CAMPAIGN_PROFILE
    domain: CampaignDomain
    flow_ml_min: PositiveNumber
    constraints: MethodConstraints
    tasks: _TaskList
    seeds: _SeedList = Field(
        default_factory=lambda: [1, 2, 3],
        description="Unique nonnegative 31-bit integer seeds, one to twenty values.",
    )
    evaluation_budget: StrictInt = Field(default=10, ge=4, le=_MAX_EVALUATION_BUDGET)
    initial_design_size: StrictInt = Field(default=3, ge=3)
    policies: list[PolicyId] = Field(
        default_factory=lambda: list(SUPPORTED_POLICY_IDS), min_length=2, max_length=2
    )
    initial_design_policy: Literal["common_coordinates_counted_per_policy_seed"] = (
        INITIAL_DESIGN_POLICY
    )
    stopping_policy: Literal["full_budget"] = STOPPING_POLICY
    fidelity: FiniteNumber = FIDELITY_VALUE
    fidelity_role: Literal["high"] = FIDELITY_ROLE
    observation_origin: Literal["simulator_latent"] = OBSERVATION_ORIGIN
    evaluation_cost: PositiveNumber
    evaluation_cost_unit: Identifier
    initial_evaluations_always_count: StrictBool = True
    utility_profile: Literal["moses-botier-1.0.0-binary64/1"] = UTILITY_PROFILE
    utility_input_definition: Literal["finite-resolution-reference/1"] = UTILITY_INPUT_DEFINITION
    analytical_success_policy: Literal["separate-strict-observation-contract"] = (
        ANALYTICAL_SUCCESS_POLICY
    )

    @field_validator("fidelity", mode="before")
    @classmethod
    def require_numeric_fidelity(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("fidelity must be the finite numeric value 1.0")
        try:
            normalized = float(value)
        except (OverflowError, ValueError) as error:
            raise ValueError("fidelity must be the finite numeric value 1.0") from error
        if not math.isfinite(normalized) or normalized != FIDELITY_VALUE:
            raise ValueError("fidelity must be the finite numeric value 1.0")
        return normalized

    @model_validator(mode="after")
    def validate_protocol(self) -> CampaignProtocolSpec:
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("task_id values must be unique")
        if len(set(self.seeds)) != len(self.seeds):
            raise ValueError("seeds must be unique")
        if len(set(self.policies)) != len(self.policies) or set(self.policies) != set(
            SUPPORTED_POLICY_IDS
        ):
            raise ValueError(f"policies must contain exactly {SUPPORTED_POLICY_IDS!r}")
        self.policies = list(SUPPORTED_POLICY_IDS)
        if self.initial_design_size < 3:
            raise ValueError(
                "initial_design_size must be at least three for the two-dimensional policy"
            )
        if not self.initial_design_size < self.evaluation_budget <= _MAX_EVALUATION_BUDGET:
            raise ValueError("initial_design_size must be below evaluation_budget, at most 50")
        if not math.isfinite(self.evaluation_budget * self.evaluation_cost):
            raise ValueError("evaluation_budget times evaluation_cost must remain finite")
        if self.initial_evaluations_always_count is not True:
            raise ValueError("initial evaluations must count toward evaluation_budget")
        if self.fidelity != FIDELITY_VALUE:
            raise ValueError("fidelity must be exactly 1.0")
        if not _all_domain_corners_feasible(self):
            raise ValueError(
                "all corners of the declared candidate domain must satisfy constraints"
            )
        if not _initial_design_is_representable(self):
            raise ValueError(
                "declared bounds cannot realize unique initial candidates in every LHS stratum"
            )
        return self


class AblationProtocolSpec(CampaignProtocolSpec):
    """Versioned protocol adding predeclared optimizer-target and noise-prior factors."""

    profile: Literal["synthetic-isocratic-ablation/1"] = Field(...)
    optimizer_target: Literal["historical_utility", "confirmed_acceptance_reward"]
    surrogate_noise_prior: Literal["default", "low_noise"]


ProtocolSpec = CampaignProtocolSpec | AblationProtocolSpec


class CandidateCoordinates(StrictModel):
    """One candidate with exactly the two declared decision coordinates."""

    strong_solvent_fraction: Annotated[FiniteNumber, Field(ge=0, le=1)]
    acquisition_duration_min: Annotated[FiniteNumber, Field(ge=1)]


def parse_campaign_protocol(spec: ProtocolSpec | dict[str, Any]) -> ProtocolSpec:
    """Parse either the frozen paired V1 protocol or the explicit ablation V2 profile.

    V1 dictionaries may omit ``profile`` and retain the original V1 defaults. Existing
    ``CampaignProtocolSpec`` objects are returned as-is, so this dispatcher does not
    reserialize or otherwise alter their canonical identity.
    """
    if isinstance(spec, AblationProtocolSpec):
        return spec
    if isinstance(spec, CampaignProtocolSpec):
        return spec
    if not isinstance(spec, dict):
        raise TypeError("campaign protocol must be a protocol model or a mapping")
    profile = spec.get("profile", CAMPAIGN_PROFILE)
    if profile == CAMPAIGN_PROFILE:
        return CampaignProtocolSpec.model_validate(spec)
    if profile == ABLATION_CAMPAIGN_PROFILE:
        return AblationProtocolSpec.model_validate(spec)
    raise ValueError(f"unsupported campaign protocol profile: {profile!r}")


def _candidate_coordinates(candidate: dict[str, Any]) -> CandidateCoordinates:
    return CandidateCoordinates.model_validate(candidate)


def _build_declaration(spec: ProtocolSpec, candidate: CandidateCoordinates) -> MethodDeclaration:
    strong_fraction = candidate.strong_solvent_fraction
    declaration = {
        "time_unit": "min",
        "flow_unit": "mL/min",
        "solvents": list(SOLVENT_IDS),
        "program": {
            "kind": "isocratic",
            "acquisition_duration": candidate.acquisition_duration_min,
            "state": {
                "flow": spec.flow_ml_min,
                "solvent_fractions": {
                    "A": 1.0 - strong_fraction,
                    "B": strong_fraction,
                },
            },
        },
        "pre_acquisition": None,
        "post_acquisition": None,
        "column": None,
        "instrument": None,
        "detector": None,
    }
    return MethodDeclaration.model_validate(declaration)


def _all_domain_corners_feasible(spec: ProtocolSpec) -> bool:
    strong = spec.domain.strong_solvent_fraction
    duration = spec.domain.acquisition_duration_min
    for strong_fraction in (strong.lower, strong.upper):
        for duration_min in (duration.lower, duration.upper):
            candidate = CandidateCoordinates(
                strong_solvent_fraction=strong_fraction,
                acquisition_duration_min=duration_min,
            )
            declaration = _build_declaration(spec, candidate)
            evaluation = evaluate_method(declaration, spec.constraints)
            if not evaluation.feasible:
                return False
    return True


def campaign_protocol_sha256(spec: ProtocolSpec | dict[str, Any]) -> str:
    """Hash the complete JSON-normalized storage binding."""
    normalized = parse_campaign_protocol(spec).model_dump(mode="json")
    return sha256(normalized)


def campaign_design_sha256(spec: ProtocolSpec | dict[str, Any]) -> str:
    """Hash the design while excluding opaque producer and contract IDs.

    Logical task IDs remain part of the scientific design identity. Rebinding
    those tasks to producer or observation-contract records in a new database
    does not change seeded candidate coordinates. The ablation profile additionally
    excludes its two declared optimizer factors so paired arms share coordinates.
    """
    protocol = parse_campaign_protocol(spec)
    normalized = protocol.model_dump(mode="json")
    normalized["tasks"] = [
        {"task_id": task["task_id"]}
        for task in sorted(normalized["tasks"], key=lambda item: item["task_id"])
    ]
    if isinstance(protocol, AblationProtocolSpec):
        normalized.pop("optimizer_target")
        normalized.pop("surrogate_noise_prior")
    return sha256(normalized)


def optimizer_configuration(spec: ProtocolSpec | dict[str, Any]) -> dict[str, Any]:
    """Return the frozen objective and backend profile selected by this protocol."""
    protocol = parse_campaign_protocol(spec)
    if isinstance(protocol, CampaignProtocolSpec) and not isinstance(
        protocol, AblationProtocolSpec
    ):
        return {
            "target_name": "utility",
            "target_profile": UTILITY_PROFILE,
            "noise_prior_params": None,
            "adapter_profile": "bo-mcp-gp-logei/1",
        }

    target_name = (
        "utility"
        if protocol.optimizer_target == "historical_utility"
        else protocol.optimizer_target
    )
    target_profile = (
        UTILITY_PROFILE
        if protocol.optimizer_target == "historical_utility"
        else CONFIRMED_ACCEPTANCE_PROFILE
    )
    noise_prior_params = (
        [1.1, 0.05] if protocol.surrogate_noise_prior == "default" else [1.1, 100.0]
    )
    return {
        "target_name": target_name,
        "target_profile": target_profile,
        "noise_prior_params": noise_prior_params,
        "adapter_profile": "bo-mcp-gp-logei-ablation/1",
    }


def _validate_seed_ordinal(spec: ProtocolSpec, seed: int, ordinal: int, *, initial: bool) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if seed not in spec.seeds:
        raise ValueError("seed must be predeclared in the protocol")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise TypeError("ordinal must be an integer")
    if initial:
        if not 0 <= ordinal < spec.initial_design_size:
            raise ValueError("initial candidate ordinal must be in [0, initial_design_size)")
    elif not spec.initial_design_size <= ordinal < spec.evaluation_budget:
        raise ValueError(
            "random candidate ordinal must be in [initial_design_size, evaluation_budget)"
        )


def _random_seed(
    spec: ProtocolSpec,
    *,
    seed: int,
    stream: str,
    ordinal: int,
    coordinate: str,
) -> int:
    entropy = {
        "campaign_design_sha256": campaign_design_sha256(spec),
        "seed": seed,
        "stream": stream,
        "ordinal": ordinal,
        "coordinate": coordinate,
    }
    digest = hashlib.sha256(canonical(entropy).encode("utf-8")).digest()
    return int.from_bytes(digest, "big")


def _numeric_design_inputs(spec: ProtocolSpec, seed: int, stream: str, ordinal: int):
    names = ("strong_solvent_fraction", "acquisition_duration_min")
    bounds = [
        (getattr(spec.domain, name).lower, getattr(spec.domain, name).upper) for name in names
    ]
    seeds = [
        _random_seed(spec, seed=seed, stream=stream, ordinal=ordinal, coordinate=name)
        for name in names
    ]
    return bounds, seeds


def _initial_design_is_representable(spec: ProtocolSpec) -> bool:
    for seed in spec.seeds:
        bounds, dimension_seeds = _numeric_design_inputs(spec, seed, "initial_lhs", 0)
        try:
            latin_hypercube_design(
                bounds=bounds, size=spec.initial_design_size, dimension_seeds=dimension_seeds
            )
        except ValueError:
            return False
    return True


def initial_candidate(
    spec: ProtocolSpec | dict[str, Any], seed: int, ordinal: int
) -> dict[str, float]:
    """Return one reproducible point from the shared two-dimensional LHS.

    Ordinals are zero-based. Each policy uses these same coordinates for a
    seed, but each policy's evaluations count separately toward its budget.
    Every call regenerates each dimension's stratum permutation and jitters
    from a SHA-256-derived standard-library ``random.Random`` seed based on the
    normalized design hash, not opaque producer or contract record IDs.
    """
    protocol = parse_campaign_protocol(spec)
    _validate_seed_ordinal(protocol, seed, ordinal, initial=True)
    bounds, dimension_seeds = _numeric_design_inputs(protocol, seed, "initial_lhs", 0)
    point = latin_hypercube_design(
        bounds=bounds, size=protocol.initial_design_size, dimension_seeds=dimension_seeds
    )[ordinal]
    return dict(zip(("strong_solvent_fraction", "acquisition_duration_min"), point, strict=True))


def random_candidate(
    spec: ProtocolSpec | dict[str, Any], seed: int, ordinal: int
) -> dict[str, float]:
    """Return one stateless random-policy candidate for a later ordinal.

    The per-coordinate RNG is derived from the normalized design hash, seed,
    ordinal and coordinate name. Replaying a candidate needs no persisted RNG
    state and does not execute an optimizer.
    """
    protocol = parse_campaign_protocol(spec)
    _validate_seed_ordinal(protocol, seed, ordinal, initial=False)
    bounds, dimension_seeds = _numeric_design_inputs(protocol, seed, "random_policy", ordinal)
    point = uniform_random_point(bounds=bounds, dimension_seeds=dimension_seeds)
    return dict(zip(("strong_solvent_fraction", "acquisition_duration_min"), point, strict=True))


def method_declaration(
    spec: ProtocolSpec | dict[str, Any], candidate: dict[str, Any]
) -> MethodDeclaration:
    """Build and feasibility-check one fixed-flow isocratic method.

    Candidate coordinates must have exact keys and lie within the declared
    closed domain. The strong-solvent fraction maps to solvent B; solvent A is
    its complement. The method has no overhead phases or equipment claims.
    Infeasible candidates raise instead of returning a declaration that could
    be mistaken for an executable campaign evaluation.
    """
    protocol = parse_campaign_protocol(spec)
    coordinates = _candidate_coordinates(candidate)
    strong = protocol.domain.strong_solvent_fraction
    duration = protocol.domain.acquisition_duration_min
    if not strong.lower <= coordinates.strong_solvent_fraction <= strong.upper:
        raise ValueError("strong_solvent_fraction is outside the declared closed domain")
    if not duration.lower <= coordinates.acquisition_duration_min <= duration.upper:
        raise ValueError("acquisition_duration_min is outside the declared closed domain")
    declaration = _build_declaration(protocol, coordinates)
    evaluation = evaluate_method(declaration, protocol.constraints)
    if not evaluation.feasible:
        codes = sorted({item.code for item in evaluation.violations})
        raise ValueError(f"candidate method is infeasible under protocol constraints: {codes}")
    return evaluation.declaration
