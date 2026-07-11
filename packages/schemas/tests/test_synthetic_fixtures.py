# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Schema validation for checked-in synthetic fixtures."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from heartwood.schemas import (
    ApprovalRecord,
    AuditEvent,
    ConfirmationRequest,
    DetectorEvidence,
    EgressAttestationRecord,
    ModelCallDecision,
    PolicyProfile,
    SkillMetadata,
)

_FIXTURES = Path("fixtures/synthetic")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_environment_probe_fixtures_have_synthetic_env_mappings() -> None:
    probe_dir = _FIXTURES / "environment-probes"
    probes = sorted(probe_dir.glob("*.json"))
    assert {path.name for path in probes} == {"ambiguous.json", "generic.json", "terra.json"}
    for probe in probes:
        payload = _read_json(probe)
        assert isinstance(payload, dict)
        assert isinstance(payload["description"], str)
        assert "Synthetic" in payload["description"] or "synthetic" in payload["description"]
        assert isinstance(payload["env"], dict)
        assert all(isinstance(key, str) for key in payload["env"])
        assert all(isinstance(value, str) for value in payload["env"].values())


def test_omop_like_csv_fixtures_have_expected_synthetic_shape() -> None:
    person_path = _FIXTURES / "omop-like" / "person.csv"
    condition_path = _FIXTURES / "omop-like" / "condition_occurrence.csv"
    with person_path.open(encoding="utf-8", newline="") as person_file:
        person_rows = list(csv.DictReader(person_file))
    with condition_path.open(encoding="utf-8", newline="") as condition_file:
        condition_rows = list(csv.DictReader(condition_file))

    assert set(person_rows[0]) == {
        "person_id",
        "gender_concept_id",
        "year_of_birth",
        "race_concept_id",
        "ethnicity_concept_id",
    }
    assert set(condition_rows[0]) == {
        "condition_occurrence_id",
        "person_id",
        "condition_concept_id",
        "condition_start_year",
    }
    assert len(person_rows) == 24
    assert len(condition_rows) == 39
    assert sum(row["condition_concept_id"] == "201826" for row in condition_rows) == 20
    assert {row["person_id"] for row in condition_rows} == {
        str(person_id) for person_id in range(1, 25)
    }


def test_policy_fixture_matches_policy_profile_schema() -> None:
    payload = _read_json(_FIXTURES / "policies" / "generic-default.json")
    policy = PolicyProfile.model_validate(payload)
    assert policy.deny_egress_by_default is True


def test_detector_fixture_matches_detector_evidence_schema() -> None:
    payload = _read_json(_FIXTURES / "detector-evidence" / "generic-platform.json")
    evidence = DetectorEvidence.model_validate(payload)
    assert evidence.detector_kind == "platform"


def test_denied_egress_fixture_matches_model_call_decision_schema() -> None:
    payload = _read_json(_FIXTURES / "egress" / "denied-attempts.json")
    decision = ModelCallDecision.model_validate(payload)
    assert decision.decision == "deny"


def test_egress_attestation_fixture_matches_attestation_schema() -> None:
    payload = _read_json(_FIXTURES / "egress" / "attestation-record.json")
    record = EgressAttestationRecord.model_validate(payload)
    assert record.decision_id == "decision-synthetic-denied-egress"


def test_skill_metadata_fixture_matches_skill_metadata_schema() -> None:
    payload = _read_json(_FIXTURES / "skills" / "omop-cohort-summary" / "metadata.json")
    metadata = SkillMetadata.model_validate(payload)
    assert metadata.dataset_types == ("omop-cdm",)
    assert metadata.trust_tier == "verified"


def test_approval_fixture_matches_approval_schema() -> None:
    payload = _read_json(_FIXTURES / "approvals" / "skill-approval.json")
    approval = ApprovalRecord.model_validate(payload)
    assert approval.target_type == "skill"


def test_confirmation_request_fixture_matches_confirmation_schema() -> None:
    payload = _read_json(_FIXTURES / "approvals" / "tool-confirmation-request.json")
    request = ConfirmationRequest.model_validate(payload)
    assert request.tool_call_id == "session-synthetic-001-toolcall-0"


def test_expected_audit_export_matches_audit_schema() -> None:
    path = _FIXTURES / "audit" / "expected-export.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    events = [AuditEvent.model_validate_json(line) for line in lines]
    assert [event.sequence for event in events] == [0, 1]
