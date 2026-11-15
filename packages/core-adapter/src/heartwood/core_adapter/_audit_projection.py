# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content-minimized audit projection of operational session events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from heartwood.core_adapter.external_actions import ExternalActionRecord
from heartwood.core_adapter.external_results import ExternalResultRecord
from heartwood.core_adapter.worker_attempts import WorkerAttemptRecord
from heartwood.schemas import JsonValue
from heartwood.session import EventKind


def project_audit_payload(kind: EventKind, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Project an operational event into its content-minimized audit representation."""
    if kind == EventKind.EXTERNAL_RESULT_RECORDED:
        release = ExternalResultRecord.model_validate_json(json.dumps(payload["record"]))
        decided = release.withdrawal or release.decision
        return {
            "release_id": str(release.proposal.release_id),
            "operation_id": str(release.proposal.operation_id),
            "proposal_sha256": release.proposal.sha256,
            "phase": release.phase,
            **_selected_audit_fields(payload, "evidence_mode"),
            "actor_id": decided.actor_id if decided else None,
            "command_id": decided.command_id if decided else None,
        }
    if kind == EventKind.EXTERNAL_ACTION_RECORDED:
        outside = ExternalActionRecord.model_validate_json(json.dumps(payload["record"]))
        decided = outside.withdrawal or outside.decision
        return {
            "operation_id": str(outside.proposal.operation_id),
            "proposal_sha256": outside.proposal.sha256,
            "phase": outside.phase,
            **_selected_audit_fields(payload, "evidence_mode"),
            "actor_id": decided.actor_id if decided else None,
            "command_id": decided.command_id if decided else None,
            "status": outside.observation.operation.status if outside.observation else None,
        }
    if kind == EventKind.WORKER_ATTEMPT_RECORDED:
        record = WorkerAttemptRecord.model_validate(payload)
        return {
            "phase": record.phase,
            "project_id": str(record.intent.project_id),
            "attempt_id": str(record.intent.attempt_id),
            "conversation_id": str(record.intent.conversation_id),
            "binding_sha256": hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    if kind == EventKind.WORKFLOW_EXECUTION_RECORDED:
        return _selected_audit_fields(
            payload,
            "run_id",
            "stage_id",
            "tool_call_id",
            "group_id",
            "status",
            "observation_fingerprint",
            "preparation_event_id",
            "execution_event_id",
        )
    if kind == EventKind.WORKFLOW_UPDATED:
        return _selected_audit_fields(
            payload,
            "command_id",
            "actor_id",
            "run_id",
            "stage_id",
            "phase",
            "revision",
            "transition",
            "approved",
            "workflow_fingerprint",
            "evidence_fingerprint",
            "assessed_stage_id",
            "experiment_fingerprint",
            "research_review_fingerprint",
            "research_correction_fingerprint",
        )
    if kind == EventKind.COMMAND_RECEIVED:
        return _selected_audit_fields(
            payload,
            "actor_id",
            "command_id",
            "command_hash",
            "command_kind",
        )
    if kind == EventKind.MODEL_CALL_DECISION_RECORDED:
        decision = payload.get("decision")
        model_profile = payload.get("model_profile")
        return {
            "decision": (
                _selected_audit_fields(
                    decision,
                    "decision_id",
                    "policy_profile_id",
                    "endpoint",
                    "capability_tier",
                    "decision",
                )
                if isinstance(decision, dict)
                else {}
            ),
            "model_profile": (
                _selected_audit_fields(
                    model_profile,
                    "backend_id",
                    "profile_id",
                    "capability_tier",
                    "action_review_mode",
                )
                if isinstance(model_profile, dict)
                else {}
            ),
        }
    if kind == EventKind.APPROVAL_RECORDED:
        tool_call_ids = payload.get("tool_call_ids")
        return {
            **_selected_audit_fields(
                payload,
                "command_id",
                "actor_id",
                "group_id",
                "decision",
                "review",
                "evidence_mode",
            ),
            "action_count": len(tool_call_ids) if isinstance(tool_call_ids, list) else 0,
        }
    if kind == EventKind.USER_MESSAGE_RECORDED:
        content = payload.get("content")
        return {
            "actor_id": payload.get("actor_id", ""),
            "command_id": payload.get("command_id", ""),
            "content_chars": len(content) if isinstance(content, str) else 0,
        }
    if kind == EventKind.AGENT_MESSAGE_EMITTED:
        content = payload.get("content")
        return {
            "content_chars": len(content) if isinstance(content, str) else 0,
        }
    if kind == EventKind.TOOL_CALL_PROPOSED:
        return _selected_audit_fields(
            payload,
            "tool_call_id",
            "tool_name",
            "risk",
        )
    if kind == EventKind.CONFIRMATION_REQUESTED:
        request = payload.get("request")
        if not isinstance(request, dict):
            return {"request": "invalid"}
        return {
            "request": _selected_audit_fields(
                request,
                "request_id",
                "group_id",
                "tool_call_id",
                "tool_name",
                "risk",
            )
        }
    if kind == EventKind.CONFIRMATION_RESOLVED:
        return _selected_audit_fields(
            payload,
            "group_id",
            "tool_call_id",
            "decision",
        )
    if kind == EventKind.TOOL_EXECUTION_RECORDED:
        return _selected_audit_fields(
            payload,
            "backend_id",
            "tool_call_id",
            "action_id",
            "tool_name",
            "exit_code",
        )
    if kind == EventKind.TASK_PLAN_UPDATED:
        tasks = payload.get("tasks")
        task_items = tasks if isinstance(tasks, list) else []
        counts: dict[str, int] = {}
        for task in task_items:
            if isinstance(task, dict):
                status = str(task.get("status", "unknown"))
                counts[status] = counts.get(status, 0) + 1
        normalized_counts: dict[str, JsonValue] = dict(counts)
        return {
            "task_count": len(task_items),
            "status_counts": normalized_counts,
        }
    if kind == EventKind.MODEL_USAGE_UPDATED:
        usage = payload.get("usage")
        return {
            "usage": (
                _selected_audit_fields(
                    usage,
                    "usage_id",
                    "model_name",
                    "call_count",
                    "prompt_tokens",
                    "completion_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "context_window",
                    "accumulated_cost",
                )
                if isinstance(usage, dict)
                else {}
            )
        }
    if kind == EventKind.SUBAGENT_UPDATED:
        subagent = payload.get("subagent")
        return {
            "subagent": (
                _selected_audit_fields(
                    subagent,
                    "invocation_id",
                    "task_id",
                    "agent_name",
                    "role_label",
                    "status",
                    "parent_session_id",
                    "parent_action_id",
                )
                if isinstance(subagent, dict)
                else {}
            )
        }
    if kind == EventKind.AGENT_LIFECYCLE_UPDATED:
        return _selected_audit_fields(payload, "status")
    if kind in {EventKind.SESSION_PAUSED, EventKind.SESSION_RESUMED, EventKind.SESSION_CLOSED}:
        return _selected_audit_fields(payload, "command_id")
    if kind == EventKind.ERROR_RECORDED:
        minimized = _selected_audit_fields(
            payload,
            "backend_id",
            "code",
            "command",
        )
        if "reason" in payload:
            minimized["reason"] = "[scrubbed]"
        return minimized
    if kind == EventKind.AUDIT_EXPORT_RECORDED:
        return _selected_audit_fields(
            payload, "event_count", "scrubbed", "experiment_export", "checkpoint_context"
        )
    if kind == EventKind.AUDIT_EVIDENCE_RECORDED:
        return _selected_audit_fields(
            payload,
            "boundary_event_id",
            "boundary_sequence",
            "mode",
            "status",
            "code",
            "checkpoint_sha256",
            "covered_through_sequence",
        )
    return {"payload_omitted": True}


def _selected_audit_fields(
    payload: Mapping[str, JsonValue],
    *names: str,
) -> dict[str, JsonValue]:
    return {name: payload[name] for name in names if name in payload}
