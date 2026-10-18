# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Offline checks reject invalid messages without trusting or echoing provider content."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from heartwood.adapters.service_conformance import (
    ServiceConformanceError,
    validate_model_catalog,
    validate_platform_message,
)

_SCHEMA = "model-metadata.schema.json"
_SECRET = "synthetic-private-value-DO-NOT-ECHO"
_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_platform_response.py"


def _metadata() -> dict[str, object]:
    return {
        "schema_version": "heartwood.platform-model-metadata.v1",
        "models": [
            {
                "connection_id": "synthetic-connection",
                "model_id": "synthetic-model",
                "protocols": ["openai"],
                "context_window_tokens": 100,
                "max_input_tokens": 80,
                "max_output_tokens": 20,
                "supports_tool_calls": None,
            }
        ],
    }


def test_offline_message_check_reports_only_structural_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_network(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Offline conformance must not establish a network connection")

    monkeypatch.setattr(socket, "create_connection", forbid_network)
    payload = _metadata()
    report = validate_platform_message(_SCHEMA, payload)
    assert report.service == "platform-message"
    assert report.item_count == 1
    assert report.checks
    output = json.dumps(asdict(report))
    assert "synthetic-model" not in output
    assert "synthetic-connection" not in output
    assert any("No platform authority" in limitation for limitation in report.limitations)
    assert any("Issuer identity" in limitation for limitation in report.limitations)
    assert any("not reservations" in limitation for limitation in report.limitations)
    assert payload == _metadata()


@pytest.mark.parametrize("name", ["unknown.schema.json", "policy-profile.schema.json", _SECRET])
def test_unknown_or_non_draft_artifact_is_not_silently_accepted(name: str) -> None:
    with pytest.raises(ServiceConformanceError) as caught:
        validate_platform_message(name, _metadata())
    assert str(caught.value) == "Draft platform message failed contract validation"
    assert _SECRET not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        1,
        "opaque",
        {"models": "wrong"},
        {"schema_version": "unknown", "models": []},
        {"models": [], "api_key": _SECRET},
        {"models": [], "approved": True},
        {"models": [], "authorization": {"approved": True}},
    ],
)
def test_invalid_message_and_provider_approval_claims_fail_safely(payload: object) -> None:
    with pytest.raises(ServiceConformanceError) as caught:
        validate_platform_message(_SCHEMA, payload)
    assert str(caught.value) == "Draft platform message failed contract validation"
    assert _SECRET not in str(caught.value)


def test_cross_field_invalid_model_is_rejected_by_the_owning_parser() -> None:
    payload = {
        "models": [
            {
                "connection_id": "synthetic",
                "model_id": "synthetic",
                "protocols": ["openai"],
                "context_window_tokens": 100,
                "max_output_tokens": 101,
            }
        ],
    }
    with pytest.raises(ServiceConformanceError):
        validate_platform_message(_SCHEMA, payload)


def test_duplicate_model_identities_are_not_counted_as_independent_metadata() -> None:
    model = {"connection_id": "synthetic", "model_id": "synthetic", "protocols": ["openai"]}
    with pytest.raises(ServiceConformanceError):
        validate_platform_message(_SCHEMA, {"models": [model, model]})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), b"secret"])
def test_non_json_inputs_fail_without_echoing_values(value: object) -> None:
    with pytest.raises(ServiceConformanceError):
        validate_platform_message(_SCHEMA, {"models": [], "private": value})


def test_circular_input_is_a_safe_validation_failure() -> None:
    payload: dict[str, object] = {"models": []}
    payload["private"] = payload
    with pytest.raises(ServiceConformanceError):
        validate_platform_message(_SCHEMA, payload)


def test_upstream_catalog_approval_hint_does_not_become_authority() -> None:
    report = validate_model_catalog({"data": [{"id": "synthetic", "approved": True}]})
    assert report.item_count == 1
    assert any("No platform authority" in limitation for limitation in report.limitations)
    assert "approved" not in json.dumps(asdict(report))


@pytest.mark.parametrize("optimized", [False, True])
def test_cli_valid_captured_message_is_content_minimized(tmp_path: Path, optimized: bool) -> None:
    capture = tmp_path / "captured-response.json"
    capture.write_text(json.dumps(_metadata()), encoding="utf-8")
    options = ["-O"] if optimized else []
    result = subprocess.run(
        [sys.executable, *options, str(_SCRIPT), _SCHEMA, str(capture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["service"] == "platform-message"
    assert report["item_count"] == 1
    assert "synthetic-model" not in result.stdout + result.stderr
    assert "synthetic-connection" not in result.stdout + result.stderr


@pytest.mark.parametrize("kind", [_SCHEMA, "model-catalog", "mcp-tools", _SECRET])
@pytest.mark.parametrize("optimized", [False, True])
def test_cli_invalid_captured_message_has_no_payload_or_traceback(
    tmp_path: Path,
    kind: str,
    optimized: bool,
) -> None:
    capture = tmp_path / "captured-response.json"
    capture.write_text(json.dumps({"models": [], "api_key": _SECRET}), encoding="utf-8")
    options = ["-O"] if optimized else []
    result = subprocess.run(
        [sys.executable, *options, str(_SCRIPT), kind, str(capture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Response could not be read or failed contract validation.\n"
    assert _SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("contents", [b"{", b"\xff", b'"synthetic-private-value-DO-NOT-ECHO"'])
def test_cli_malformed_captured_bytes_are_sanitized(tmp_path: Path, contents: bytes) -> None:
    capture = tmp_path / "captured-response.json"
    capture.write_bytes(contents)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), _SCHEMA, str(capture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Response could not be read or failed contract validation.\n"


def test_cli_unreadable_capture_does_not_echo_its_path(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), _SCHEMA, str(tmp_path / _SECRET)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert _SECRET not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_excessive_json_nesting_fails_without_a_traceback(tmp_path: Path) -> None:
    capture = tmp_path / "captured-response.json"
    capture.write_text("[" * 1500 + json.dumps(_SECRET) + "]" * 1500, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), _SCHEMA, str(capture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Response could not be read or failed contract validation.\n"


def test_observed_eligibility_is_not_reported_as_verified_authority() -> None:
    payload = {
        "models": [
            {
                "connection_id": "synthetic",
                "model_id": "synthetic",
                "protocols": ["openai"],
                "data_eligibility": [
                    {
                        "classification": "restricted",
                        "allowed": True,
                        "authority_ref": "claimed-platform",
                        "policy_ref": "claimed-policy",
                        "source_ref": "claimed-evidence",
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ],
    }
    report = validate_platform_message(_SCHEMA, payload)
    assert any("Issuer identity" in limitation for limitation in report.limitations)
    assert "claimed-platform" not in json.dumps(asdict(report))


def test_optimized_cli_preserves_cross_field_validation(tmp_path: Path) -> None:
    capture = tmp_path / "captured-response.json"
    capture.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "connection_id": "synthetic",
                        "model_id": "synthetic",
                        "protocols": ["openai"],
                        "context_window_tokens": 100,
                        "max_input_tokens": 101,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-O", str(_SCRIPT), _SCHEMA, str(capture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Response could not be read or failed contract validation.\n"
