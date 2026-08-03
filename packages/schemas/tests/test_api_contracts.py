# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for strict public request and response contracts."""

from __future__ import annotations

from typing import assert_type

import pytest
from pydantic import ValidationError

from heartwood.schemas import (
    ModelCatalogRequest,
    SessionSummaryResponse,
    SpecialistSettingsResponse,
    api_contract_schema,
    api_response,
)


def test_request_models_reject_coercion_and_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Input should be a valid boolean"):
        ModelCatalogRequest.model_validate(
            {
                "connection_id": "openai",
                "refresh": "true",
            },
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelCatalogRequest.model_validate(
            {
                "connection_id": "openai",
                "implementation_detail": "not-public",
            },
        )


def test_response_validation_preserves_precise_static_types() -> None:
    response = api_response(
        SessionSummaryResponse,
        {
            "session_id": "session-1",
            "title": "Synthetic analysis",
            "status": "idle",
            "created_at": "2026-07-25T12:00:00Z",
            "updated_at": "2026-07-25T12:05:00Z",
            "event_count": 4,
        },
    )

    assert_type(response, SessionSummaryResponse)
    assert_type(response["session_id"], str)
    assert_type(response["event_count"], int)
    assert response["title"] == "Synthetic analysis"


def test_response_validation_rejects_contract_drift() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        api_response(
            SessionSummaryResponse,
            {
                "session_id": "session-1",
                "title": "Synthetic analysis",
                "status": "idle",
                "created_at": "2026-07-25T12:00:00Z",
                "updated_at": "2026-07-25T12:05:00Z",
                "event_count": 4,
                "internal_path": "/private/session-1",
            },
        )


def test_api_contract_schema_contains_requests_and_responses() -> None:
    schema = api_contract_schema()
    definitions = schema["$defs"]

    assert isinstance(definitions, dict)
    assert "ModelCatalogRequest" in definitions
    assert "SessionSummaryResponse" in definitions
    assert "SpecialistSettingsResponse" in definitions
    assert schema["anyOf"]


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("model_route", "external-model"),
        ("tools", ["terminal"]),
        ("capability", "project-actions"),
        ("availability", "unavailable"),
        ("unavailable_reason", "Not unavailable."),
        ("permission_mode", "bypass"),
        ("presentation_summary", ""),
        ("max_iterations", 0),
        ("max_budget_usd", 0.0),
    ],
)
def test_specialist_response_rejects_unsafe_contract_drift(
    field: str,
    unsafe_value: object,
) -> None:
    role: dict[str, object] = {
        "specialist_id": "bounded-reviewer",
        "label": "Bounded Reviewer",
        "description": "Reviews supplied synthetic evidence.",
        "presentation_summary": "Advisory · Uses the active model · Up to 4 steps",
        "capability": "advisory",
        "availability": "available",
        "unavailable_reason": None,
        "model_route": "inherit",
        "tools": [],
        "skills": [],
        "permission_mode": "always_confirm",
        "max_iterations": 4,
        "max_budget_usd": 1.0,
    }
    api_response(SpecialistSettingsResponse, {"specialists": [role]})
    role[field] = unsafe_value

    with pytest.raises(ValidationError):
        api_response(
            SpecialistSettingsResponse,
            {"specialists": [role]},
        )
