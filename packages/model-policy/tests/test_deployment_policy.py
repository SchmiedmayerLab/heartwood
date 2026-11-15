# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deployment policy admits only narrower project authority."""

import pytest

from heartwood.model_policy import PolicyInputError, validate_policy_restriction
from heartwood.schemas import PolicyProfile


def _ceiling() -> PolicyProfile:
    return PolicyProfile(
        policy_id="deployment",
        platform_id="generic",
        allowed_model_endpoints=("https://model.example/v1/chat/completions",),
        allowed_model_catalog_endpoints=("https://model.example/v1/models",),
        allowed_capability_tiers=("supervised", "experimental"),
        allowed_action_confirmation_modes=("always-confirm",),
        credential_allowlist=("MODEL_KEY",),
        aggregate_count_floor=30,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deny_egress_by_default", False),
        ("platform_id", "other"),
        ("allowed_model_endpoints", ("https://other.example/v1/chat/completions",)),
        ("allowed_model_catalog_endpoints", ("https://other.example/v1/models",)),
        ("allowed_capability_tiers", ("autonomous",)),
        ("allowed_action_confirmation_modes", ("confirm-risky",)),
        ("credential_allowlist", ("OTHER_KEY",)),
        ("aggregate_count_floor", 20),
    ],
)
def test_deployment_policy_rejects_every_authority_expansion(field: str, value: object) -> None:
    ceiling = _ceiling()
    profile = PolicyProfile.model_validate({**ceiling.model_dump(), field: value})
    with pytest.raises(PolicyInputError, match="deployment policy"):
        validate_policy_restriction(profile, ceiling)


def test_deployment_policy_accepts_narrowing_and_equivalent_endpoint_spelling() -> None:
    ceiling = _ceiling()
    profile = ceiling.model_copy(
        update={
            "policy_id": "project",
            "allowed_model_endpoints": ("HTTPS://MODEL.EXAMPLE/v1/chat/completions",),
            "allowed_model_catalog_endpoints": (),
            "allowed_capability_tiers": ("supervised",),
            "credential_allowlist": (),
            "aggregate_count_floor": 40,
        }
    )
    validate_policy_restriction(profile, ceiling)


def test_deployment_policy_itself_must_deny_unlisted_egress() -> None:
    with pytest.raises(PolicyInputError, match="deny-by-default"):
        validate_policy_restriction(
            _ceiling(), _ceiling().model_copy(update={"deny_egress_by_default": False})
        )


@pytest.mark.parametrize("field", ["allowed_model_endpoints", "allowed_model_catalog_endpoints"])
def test_deployment_policy_rejects_malformed_endpoints_even_in_matching_profiles(
    field: str,
) -> None:
    invalid = _ceiling().model_copy(update={field: ("https://user:secret@example/v1",)})
    with pytest.raises(PolicyInputError, match="userinfo"):
        validate_policy_restriction(invalid, invalid)
