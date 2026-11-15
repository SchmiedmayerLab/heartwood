# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""One rule per policy field for restricting, combining and reporting authority."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

from heartwood.model_policy._engine import PolicyInputError, normalize_endpoint
from heartwood.schemas import PolicyProfile

type RestrictionRule = Literal[
    "no-authority", "equal", "required", "endpoint-subset", "subset", "optional-subset", "minimum"
]


@dataclass(frozen=True, slots=True)
class PolicyRestriction:
    """How one policy field limits authority and how interfaces report it."""

    rule: RestrictionRule
    # The name every interface reports the field under; None for fields without authority.
    reported_as: str | None = None
    # The omitted value already permits everything, so a manifest that omits the field
    # inherits its ceiling and a project stores the field only when narrower.
    absent_means_any: bool = False
    # Setup selects the field with a model route, so a platform's routes widen it.
    route_selected: bool = False
    # The value an empty intersection falls back to, when the schema requires an entry.
    fallback: tuple[str, ...] = ()
    # A profile list that shares no entry with the ceiling stays as listed, so validation
    # refuses the profile instead of restricting it to an empty list.
    keep_disjoint: bool = False


POLICY_RESTRICTIONS: Mapping[str, PolicyRestriction] = MappingProxyType(
    {
        "schema_version": PolicyRestriction("no-authority"),
        "policy_id": PolicyRestriction("no-authority", "policy_id"),
        "platform_id": PolicyRestriction("equal", "platform_id"),
        "deny_egress_by_default": PolicyRestriction("required", "deny_egress_by_default"),
        "allowed_model_endpoints": PolicyRestriction(
            "endpoint-subset", "model_endpoints", route_selected=True
        ),
        "allowed_model_catalog_endpoints": PolicyRestriction(
            "endpoint-subset", "model_catalog_endpoints", route_selected=True
        ),
        "allowed_capability_tiers": PolicyRestriction("subset", "capability_tiers"),
        "allowed_action_review_modes": PolicyRestriction(
            "subset", "action_review_modes", absent_means_any=True, fallback=("review-all",)
        ),
        "credential_allowlist": PolicyRestriction(
            "subset", "credential_references", route_selected=True
        ),
        "allowed_credential_backends": PolicyRestriction(
            "subset", "credential_backends", absent_means_any=True
        ),
        "allowed_checkpoint_signer_profiles": PolicyRestriction(
            "optional-subset", "checkpoint_signer_profiles", absent_means_any=True
        ),
        "allowed_data_destinations": PolicyRestriction(
            "optional-subset", absent_means_any=True, keep_disjoint=True
        ),
        "aggregate_count_floor": PolicyRestriction("minimum", "aggregate_count_floor"),
        "notes": PolicyRestriction("no-authority"),
    }
)


def validate_policy_restriction(profile: PolicyProfile, ceiling: PolicyProfile) -> None:
    """Reject project rules that grant authority absent from a deployment policy.

    The caller supplies the ceiling from trusted launch configuration. Neither a
    project file nor a worker proposal can select or replace it.
    """
    for field, restriction in POLICY_RESTRICTIONS.items():
        value, limit = getattr(profile, field), getattr(ceiling, field)
        match restriction.rule:
            case "no-authority":
                permitted = True
            case "equal":
                permitted = value == limit
            case "required":
                permitted = value is True and limit is True
            case "endpoint-subset":
                permitted = {normalize_endpoint(item) for item in value} <= {
                    normalize_endpoint(item) for item in limit
                }
            case "subset":
                permitted = set(value) <= set(limit)
            case "optional-subset":
                # An absent list permits any entry, which is wider than every list.
                permitted = limit is None or (value is not None and set(value) <= set(limit))
            case "minimum":
                permitted = value >= limit
        if not permitted:
            raise PolicyInputError(f"project {field} exceeds deployment policy", field=field)


def restrict_policy(profile: PolicyProfile, ceiling: PolicyProfile) -> PolicyProfile:
    """Return the profile limited to what the ceiling also permits, never more."""
    update: dict[str, object] = {}
    for field, restriction in POLICY_RESTRICTIONS.items():
        value, limit = getattr(profile, field), getattr(ceiling, field)
        match restriction.rule:
            case "endpoint-subset":
                permitted = {normalize_endpoint(item) for item in limit}
                update[field] = tuple(
                    item for item in value if normalize_endpoint(item) in permitted
                )
            case "subset" | "optional-subset" if limit is not None:
                both = tuple(item for item in limit if value is None or item in value)
                update[field] = (
                    both or restriction.fallback or (value if restriction.keep_disjoint else ())
                )
            case "minimum":
                update[field] = max(value, limit)
            case _:
                # Equality and requirements are checked, not combined; the rest has no authority.
                pass
    return profile.model_copy(update=update)


def widen_route_fields(base: PolicyProfile, routes: Iterable[PolicyProfile]) -> PolicyProfile:
    """Return the base policy with each route-selected field permitting what any route does."""
    policies = (base, *routes)
    update: dict[str, object] = {}
    for field, restriction in POLICY_RESTRICTIONS.items():
        if not restriction.route_selected:
            continue
        if restriction.rule not in {"endpoint-subset", "subset"}:
            raise ValueError(f"route-selected field {field} is not a list of entries")
        update[field] = tuple(
            dict.fromkeys(item for policy in policies for item in getattr(policy, field))
        )
    return base.model_copy(update=update)


def inherit_absent_fields(
    requested: PolicyProfile, ceiling: PolicyProfile, present: Iterable[str]
) -> PolicyProfile:
    """Let each omitted field that would permit anything follow the ceiling instead."""
    named = set(present)
    return requested.model_copy(
        update={
            field: getattr(ceiling, field)
            for field, restriction in POLICY_RESTRICTIONS.items()
            if restriction.absent_means_any and field not in named
        }
    )


def policy_report(policy: PolicyProfile) -> dict[str, object]:
    """Return every reported policy field under its interface name."""
    report: dict[str, object] = {}
    for field, restriction in POLICY_RESTRICTIONS.items():
        if restriction.reported_as is not None:
            value = getattr(policy, field)
            report[restriction.reported_as] = (
                list(cast(tuple[object, ...], value)) if isinstance(value, tuple) else value
            )
    return report


def inherited_fields(policy: PolicyProfile, ceiling: PolicyProfile) -> tuple[str, ...]:
    """Return fields a project omits because omission already inherits the same ceiling."""
    return tuple(
        field
        for field, restriction in POLICY_RESTRICTIONS.items()
        if restriction.absent_means_any and getattr(policy, field) == getattr(ceiling, field)
    )
