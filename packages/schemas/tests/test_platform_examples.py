# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Worked examples verify native requests, exact reviewed bytes, and shared projections."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from heartwood.schemas import (
    PlanningOverview,
    platform_schema_documents,
    verify_reviewed_operation_set,
)

_EXAMPLES = Path(__file__).parents[3] / "fixtures" / "synthetic" / "platform-contract"


@pytest.mark.parametrize("kind", ["exploration", "batch", "inference"])
def test_worked_examples_bind_real_reviewed_and_native_bytes(kind: str) -> None:
    content = (_EXAMPLES / f"{kind}.overview.json").read_bytes()
    overview = PlanningOverview.model_validate_json(content)
    Draft202012Validator(
        platform_schema_documents()["planning-overview.schema.json"],
        format_checker=FormatChecker(),
    ).validate(json.loads(content))
    for envelope in overview.operation_sets:
        verify_reviewed_operation_set(envelope, (_EXAMPLES / envelope.document_ref).read_bytes())
        for operation in envelope.proposal.operations:
            request = operation.request
            exact_bytes = (_EXAMPLES / request.request_ref).read_bytes()
            assert hashlib.sha256(exact_bytes).hexdigest() == request.request_sha256
    assert overview.estimates[0].total == overview.operation_sets[0].proposal.estimates[0].total
    assert overview.researcher_decisions[0].decision == "approved"
    if kind == "batch":
        assert overview.authorizations[0].status == "denied"
        assert overview.operations[0].status == "denied"
        assert overview.budgets[0].available == "-1"
    if kind == "inference":
        assert overview.estimates[0].total.expected is None
        assert overview.budgets[0].available is None
    if kind == "exploration":
        assert overview.estimates[0].element_estimates[0].included_in_total
        assert overview.authorizations[0].reservation_ref is not None
        assert overview.operations[0].status == "running"
