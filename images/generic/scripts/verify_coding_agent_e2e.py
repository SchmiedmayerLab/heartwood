# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verify and summarize one real-model Heartwood coding-agent acceptance run."""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from heartwood.audit import AuditLog
from heartwood.session import SessionEvent

_TEST_ID = "heartwood.coding-agent-e2e.v1"
_REQUIRED_EVENT_KINDS = {
    "confirmation.requested",
    "confirmation.resolved",
    "model_call.decision.recorded",
    "tool.execution.recorded",
    "tool_call.proposed",
}


def verify_run(
    *,
    events_path: Path,
    audit_path: Path,
    artifact_path: Path,
    replay_path: Path,
    inference_path: Path,
) -> dict[str, object]:
    """Validate the complete acceptance contract and return its evidence summary."""
    events = tuple(
        SessionEvent.model_validate_json(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line
    )
    if not events:
        raise ValueError("coding-agent session has no events")
    if [event.sequence for event in events] != list(range(len(events))):
        raise ValueError("coding-agent session event sequence is not contiguous")
    kinds = {str(event.kind) for event in events}
    missing = sorted(_REQUIRED_EVENT_KINDS - kinds)
    if missing:
        raise ValueError(f"coding-agent session is missing events: {', '.join(missing)}")
    errors = [
        str(event.payload.get("reason", "unknown"))
        for event in events
        if event.kind == "error.recorded"
    ]
    if errors:
        raise ValueError(f"coding-agent session recorded errors: {errors}")

    requested = {
        str(request["tool_call_id"])
        for event in events
        if event.kind == "confirmation.requested"
        and (request := _mapping(event.payload.get("request")))
        and "tool_call_id" in request
    }
    resolved = {
        str(event.payload.get("tool_call_id"))
        for event in events
        if event.kind == "confirmation.resolved"
    }
    if requested != resolved:
        raise ValueError("coding-agent session has unresolved or unmatched approvals")
    decisions = {
        str(event.payload.get("decision"))
        for event in events
        if event.kind == "confirmation.resolved"
    }
    if decisions != {"approved", "denied"}:
        raise ValueError(
            "coding-agent qualification must record one approved and one denied action set: "
            f"{sorted(decisions)}"
        )

    tool_executions = [event for event in events if event.kind == "tool.execution.recorded"]
    terminal_executions = [
        event for event in tool_executions if event.payload.get("tool_name") == "terminal"
    ]
    if not 1 <= len(tool_executions) <= 3:
        raise ValueError("coding-agent session must have one to three tool executions")
    if not 1 <= len(terminal_executions) <= 3:
        raise ValueError("coding-agent session must execute the terminal tool")
    if any(not isinstance(event.payload.get("exit_code"), int) for event in tool_executions):
        raise ValueError("coding-agent tool execution has no valid exit code")
    if any(event.payload["exit_code"] != 0 for event in tool_executions):
        raise ValueError("coding-agent tool execution failed")
    completed_with_message = any(
        event.kind == "agent_message.emitted"
        and isinstance(event.payload.get("content"), str)
        and bool(str(event.payload["content"]).strip())
        for event in events
    )
    completed_with_finish = any(
        event.payload.get("tool_name") == "finish" and event.payload.get("exit_code") == 0
        for event in tool_executions
    )
    if not completed_with_message and not completed_with_finish:
        raise ValueError("coding-agent session has no successful completion action or message")

    routes = [
        decision.get("decision")
        for event in events
        if event.kind == "model_call.decision.recorded"
        and (decision := _mapping(event.payload.get("decision")))
    ]
    if not routes or set(routes) != {"allow"}:
        raise ValueError(f"coding-agent model route was not consistently allowed: {routes}")
    confirmation_modes = {
        profile.get("action_confirmation_mode")
        for event in events
        if event.kind == "model_call.decision.recorded"
        and (profile := _mapping(event.payload.get("model_profile")))
    }
    if confirmation_modes != {"always-confirm"}:
        raise ValueError(
            f"coding-agent session used unexpected confirmation modes: {confirmation_modes}"
        )

    cohort = json.loads(artifact_path.read_text(encoding="utf-8"))
    summary = cohort["summary"]
    expected_summary = {
        "source_participant_count": 24,
        "participant_count": 20,
        "source_condition_occurrence_count": 39,
        "condition_occurrence_count": 35,
    }
    observed_summary = {key: summary.get(key) for key in expected_summary}
    if observed_summary != expected_summary:
        raise ValueError(f"coding-agent artifact is incorrect: {observed_summary}")
    if cohort["quality_checks"].get("aggregate_only_output") is not True:
        raise ValueError("coding-agent artifact contains row-level output")
    if cohort["export_guard"].get("exportable") is not True:
        raise ValueError("coding-agent artifact unexpectedly failed its count floor")
    if not artifact_path.read_bytes().endswith(b"\n"):
        raise ValueError("coding-agent artifact does not end with a newline")
    exact_path = artifact_path.with_name("heartwood-exact-output.txt")
    if not exact_path.is_file() or exact_path.read_bytes() != b"heartwood-agent-exact-ok\n":
        raise ValueError("coding-agent exact-content artifact is incorrect")
    rejected_path = artifact_path.with_name("heartwood-rejected-output.txt")
    if rejected_path.exists():
        raise ValueError("coding-agent rejected action modified the project")

    inference = json.loads(inference_path.read_text(encoding="utf-8"))
    if inference.get("content_nonempty") is not True:
        raise ValueError("direct model inference did not return content")

    replay = replay_path.read_text(encoding="utf-8")
    if (
        "Tool terminal exit=0" not in replay
        or "Action set approved" not in replay
        or "Action set denied" not in replay
    ):
        raise ValueError("fresh-process replay is missing the approved or denied action set")

    audit = AuditLog(audit_path)
    audit_events = audit.read()
    audit.verify(audit_events)
    if len(audit_events) != len(events):
        raise ValueError("audit export and replay event counts disagree")
    if [event.event_type for event in audit_events] != [str(event.kind) for event in events]:
        raise ValueError("audit export and replay event kinds disagree")
    audit_text = audit_path.read_text(encoding="utf-8")
    for sensitive_value in (
        str(artifact_path.resolve().parent),
        "target-condition-concept-id",
        "Call the terminal tool",
        "heartwood-agent-exact-ok",
        "this-action-must-remain-rejected",
    ):
        if sensitive_value in audit_text:
            raise ValueError("audit export contains unsanitized task content")

    return {
        "event_count": len(events),
        "audit_event_count": len(audit_events),
        "tool_execution_count": len(tool_executions),
        "checks": {
            "model_loaded_and_inferred": True,
            "tool_call_proposed": True,
            "grouped_approval_recorded": True,
            "grouped_rejection_recorded": True,
            "file_modified_and_verified": True,
            "exact_content_verified": True,
            "fresh_process_replay_verified": True,
            "audit_export_verified": True,
        },
    }


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _configuration(root: Path, configuration_id: str) -> dict[str, Any]:
    matrix_path = Path(
        os.environ.get(
            "HEARTWOOD_GPU_COMPATIBILITY_MATRIX",
            root / "images/gpu/compatibility.toml",
        )
    )
    with matrix_path.open("rb") as file:
        matrix = tomllib.load(file)
    for item in matrix.get("configurations", []):
        if isinstance(item, dict) and item.get("configuration_id") == configuration_id:
            return item
    raise ValueError(f"unknown GPU qualification configuration: {configuration_id}")


def _report(summary: dict[str, object], root: Path) -> dict[str, object]:
    configuration_id = os.environ.get("HEARTWOOD_GPU_CONFIGURATION_ID")
    configuration = _configuration(root, configuration_id) if configuration_id is not None else None
    runtime_metadata_path = os.environ.get("HEARTWOOD_QUALIFICATION_RUNTIME_METADATA")
    runtime_metadata = (
        json.loads(Path(runtime_metadata_path).read_text(encoding="utf-8"))
        if runtime_metadata_path
        else {}
    )
    return {
        "schema_version": "heartwood.coding-agent-qualification.v1",
        "qualification_test": _TEST_ID,
        "status": "passed",
        "recorded_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "platform": os.environ.get("HEARTWOOD_PLATFORM", "generic"),
        "configuration_id": configuration_id,
        "configuration": configuration,
        "runtime": runtime_metadata,
        "model": {
            "repository": os.environ.get("HEARTWOOD_QUALIFICATION_MODEL_REPOSITORY"),
            "revision": os.environ.get("HEARTWOOD_QUALIFICATION_MODEL_REVISION"),
            "runtime_profile": os.environ.get("HEARTWOOD_LOCAL_RUNTIME_PROFILE"),
        },
        **summary,
    }


def main() -> int:
    """Run acceptance checks and write a portable qualification record."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--inference", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    summary = verify_run(
        events_path=args.events,
        audit_path=args.audit,
        artifact_path=args.artifact,
        replay_path=args.replay,
        inference_path=args.inference,
    )
    report = _report(summary, args.root.resolve())
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.report.chmod(0o600)
    print(f"Heartwood coding-agent qualification passed: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
