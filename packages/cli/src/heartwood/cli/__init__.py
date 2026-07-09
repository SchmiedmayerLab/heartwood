# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The ``heartwood`` command-line interface, the primary interaction surface."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from heartwood.compliance import ReviewerPacketGenerator
from heartwood.gateway import ProviderConfigError, SessionGateway, load_provider_config
from heartwood.session import CommandKind, EventKind, JsonValue, SessionCommand, SessionEvent

__all__ = ["__version__", "main"]

__version__ = "0.0.0"

_PROG = "heartwood"
_DEFAULT_WORKSPACE = Path(".heartwood") / "sessions"
_DEFAULT_MODEL_ENDPOINT = "https://model.local.invalid/v1/chat/completions"
_DEFAULT_LOOPBACK_MODEL_ENDPOINT = "http://127.0.0.1:8765/v1/chat/completions"
_DEFAULT_PROVIDER_CONFIG = Path("images") / "generic" / "providers" / "provider-routes.example.toml"
_DEFAULT_FIXTURE_ROOT = Path("fixtures") / "synthetic"


def _format_detection(event: SessionEvent, *, workspace: Path) -> str:
    """Render a detection event as a plain-language, propose-not-commit report."""
    platform = _mapping_payload(event.payload["platform"], "platform")
    dataset = _mapping_payload(event.payload["dataset"], "dataset")
    platform_confidence = _float_payload(platform["confidence"], "platform.confidence")
    dataset_confidence = _float_payload(dataset["confidence"], "dataset.confidence")
    lines = [
        "Heartwood - environment detection",
        "",
        "This is a proposal only. Nothing loads or runs without your confirmation.",
        "",
        f"Session: {event.session_id}",
        f"State: {workspace}",
        "",
        f"Platform: {platform['adapter_id']} (confidence {platform_confidence:.2f})",
    ]
    lines += [
        f"  - {item}" for item in _string_list_payload(platform["evidence"], "platform.evidence")
    ]
    lines += [
        "",
        f"Dataset: {dataset['dataset_type']} (confidence {dataset_confidence:.2f})",
    ]
    lines += [
        f"  - {item}" for item in _string_list_payload(dataset["evidence"], "dataset.evidence")
    ]
    return "\n".join(lines)


def _format_transcript(events: Sequence[SessionEvent]) -> str:
    """Render session events as a stable terminal transcript."""
    lines = [_format_event(event) for event in events]
    return "\n".join(line for line in lines if line)


def _format_event(event: SessionEvent) -> str:
    kind = _event_kind(event)
    if kind == EventKind.COMMAND_RECEIVED.value:
        return f"[{event.sequence:03d}] Command received: {event.payload.get('command_id', '')}"
    if kind == EventKind.DETECTION_PROPOSED.value:
        dataset = _mapping_payload(event.payload["dataset"], "dataset")
        platform = _mapping_payload(event.payload["platform"], "platform")
        return (
            f"[{event.sequence:03d}] Detection proposed: "
            f"platform={platform['adapter_id']} dataset={dataset['dataset_type']}"
        )
    if kind == EventKind.APPROVAL_RECORDED.value:
        approval = _mapping_payload(event.payload["approval"], "approval")
        return (
            f"[{event.sequence:03d}] Approval recorded: "
            f"{approval['target_type']} {approval['target_id']} {approval['decision']}"
        )
    if kind == EventKind.MODEL_CALL_DECISION_RECORDED.value:
        decision = _mapping_payload(event.payload["decision"], "decision")
        line = (
            f"[{event.sequence:03d}] Model call: {decision['decision']} "
            f"endpoint={decision['endpoint']} reason={decision['reason']}"
        )
        response_metadata = event.payload.get("response_metadata")
        if isinstance(response_metadata, dict):
            model = response_metadata.get("model", "unknown")
            status = response_metadata.get("status", "unknown")
            line = f"{line} model={model} status={status}"
        return line
    if kind == EventKind.AGENT_MESSAGE_EMITTED.value:
        return f"[{event.sequence:03d}] Agent: {event.payload.get('content', '')}"
    if kind == EventKind.TOOL_CALL_PROPOSED.value:
        return (
            f"[{event.sequence:03d}] Tool proposed: {event.payload.get('tool_name', '')} "
            f"risk={event.payload.get('risk', '')}"
        )
    if kind == EventKind.CONFIRMATION_REQUESTED.value:
        request = _mapping_payload(event.payload["request"], "request")
        return (
            f"[{event.sequence:03d}] Confirmation requested: {request['tool_name']} "
            f"risk={request['risk']} id={request['request_id']}"
        )
    if kind == EventKind.CONFIRMATION_RESOLVED.value:
        return (
            f"[{event.sequence:03d}] Confirmation resolved: "
            f"{event.payload.get('tool_call_id', '')} {event.payload.get('decision', '')}"
        )
    if kind == EventKind.TOOL_EXECUTION_RECORDED.value:
        return (
            f"[{event.sequence:03d}] Tool execution: {event.payload.get('tool_name', '')} "
            f"exit={event.payload.get('exit_code', '')}"
        )
    if kind == EventKind.SESSION_PAUSED.value:
        return f"[{event.sequence:03d}] Session paused"
    if kind == EventKind.SESSION_RESUMED.value:
        return f"[{event.sequence:03d}] Session resumed"
    if kind == EventKind.AUDIT_EXPORT_RECORDED.value:
        return (
            f"[{event.sequence:03d}] Audit export: {event.payload.get('path', '')} "
            f"events={event.payload.get('event_count', '')}"
        )
    if kind == EventKind.ERROR_RECORDED.value:
        return f"[{event.sequence:03d}] Error: {event.payload.get('reason', '')}"
    return f"[{event.sequence:03d}] {kind}"


def _mapping_payload(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        msg = f"expected {name} payload to be an object"
        raise TypeError(msg)
    return value


def _float_payload(value: JsonValue, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"expected {name} payload to be numeric"
        raise TypeError(msg)
    return float(value)


def _string_list_payload(value: JsonValue, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        msg = f"expected {name} payload to be a string list"
        raise TypeError(msg)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            msg = f"expected {name} payload to be a string list"
            raise TypeError(msg)
        items.append(item)
    return tuple(items)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Compliance-first coding harness for sensitive biomedical research data.",
    )
    parser.add_argument("--version", action="version", version=f"{_PROG} {__version__}")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=_DEFAULT_WORKSPACE,
        help="Directory for local session state.",
    )
    parser.add_argument("--session-id", default="session-local", help="Session identifier.")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.add_parser(
        "detect",
        help="Detect the platform and propose next steps.",
        description="Inspect environment markers and propose the platform. Propose-not-commit.",
    )
    chat = subparsers.add_parser(
        "chat",
        help="Open a terminal chat turn or pass one prompt.",
        description="Run chat turns over the shared session event stream.",
    )
    chat.add_argument("--prompt", help="Run one chat turn instead of opening the prompt loop.")
    run = subparsers.add_parser(
        "run",
        help="Run the synthetic workflow through policy-gated model-call events.",
    )
    run.add_argument("--prompt", default="run the synthetic workflow", help="Run instruction.")
    run.add_argument(
        "--endpoint",
        default=_DEFAULT_MODEL_ENDPOINT,
        help="Model endpoint to evaluate under the active policy profile.",
    )
    run.add_argument(
        "--local-model",
        action="store_true",
        help="Invoke the allowlisted loopback model endpoint before the agent turn.",
    )
    run.add_argument(
        "--provider-config",
        type=Path,
        default=Path(os.environ.get("HEARTWOOD_PROVIDER_CONFIG", str(_DEFAULT_PROVIDER_CONFIG))),
        help="Provider route configuration with file-based secret references.",
    )
    run.add_argument(
        "--provider-route",
        help="Provider route id to select from --provider-config.",
    )
    for name, help_text in (
        ("approve", "Approve a skill, egress decision, model call, or tool call."),
        ("deny", "Deny a skill, egress decision, model call, or tool call."),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument(
            "--target-type",
            choices=("skill", "egress", "model-call", "tool-call"),
            required=True,
        )
        command.add_argument("--target-id", required=True)
        command.add_argument("--reason")
    subparsers.add_parser("pause", help="Pause the current session.")
    subparsers.add_parser("resume", help="Resume the current session.")
    subparsers.add_parser("replay", help="Replay the persisted session event stream.")
    audit = subparsers.add_parser("audit", help="Audit-log operations.")
    audit_subparsers = audit.add_subparsers(dest="audit_command", metavar="<audit-command>")
    audit_export = audit_subparsers.add_parser("export", help="Export a scrubbed audit JSONL file.")
    audit_export.add_argument("--output", type=Path, help="Optional copy destination.")
    reviewer = subparsers.add_parser("reviewer", help="Reviewer packet operations.")
    reviewer_subparsers = reviewer.add_subparsers(
        dest="reviewer_command",
        metavar="<reviewer-command>",
    )
    packet = reviewer_subparsers.add_parser(
        "packet",
        help="Generate a synthetic-only reviewer packet.",
    )
    packet.add_argument("--output", type=Path, default=Path("compliance") / "reviewer-packet")
    packet.add_argument("--fixture-root", type=Path, default=_DEFAULT_FIXTURE_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``heartwood`` command and return a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    gateway = SessionGateway(workspace=args.workspace)
    gateway.start()
    try:
        if args.command == "detect":
            return _handle_detect(gateway, workspace=args.workspace, session_id=args.session_id)
        if args.command == "chat":
            if args.prompt is not None:
                command = _command(
                    gateway,
                    session_id=args.session_id,
                    kind=CommandKind.CHAT,
                    payload={"prompt": args.prompt},
                )
                print(_format_transcript(gateway.handle(command).events))
                return 0
            return _interactive_chat(gateway, session_id=args.session_id)
        if args.command == "run":
            if args.local_model and args.provider_route:
                parser.error("--provider-route cannot be combined with --local-model")
            endpoint = (
                _DEFAULT_LOOPBACK_MODEL_ENDPOINT
                if args.local_model and args.endpoint == _DEFAULT_MODEL_ENDPOINT
                else args.endpoint
            )
            provider_route: dict[str, JsonValue] | None = None
            if args.provider_route:
                try:
                    route = load_provider_config(args.provider_config).route(args.provider_route)
                except ProviderConfigError as error:
                    parser.error(str(error))
                endpoint = route.endpoint
                provider_route = cast(dict[str, JsonValue], route.safe_metadata())
            run_payload: dict[str, JsonValue] = {
                "prompt": args.prompt,
                "endpoint": endpoint,
                "invoke_model": args.local_model,
            }
            if provider_route is not None:
                run_payload["provider_route"] = provider_route
            command = _command(
                gateway,
                session_id=args.session_id,
                kind=CommandKind.RUN,
                payload=run_payload,
            )
            print(_format_transcript(gateway.handle(command).events))
            return 0
        if args.command in {"approve", "deny"}:
            kind = CommandKind.APPROVE if args.command == "approve" else CommandKind.DENY
            approval_payload: dict[str, JsonValue] = {
                "target_type": args.target_type,
                "target_id": args.target_id,
            }
            if args.reason:
                approval_payload["reason"] = args.reason
            command = _command(
                gateway,
                session_id=args.session_id,
                kind=kind,
                payload=approval_payload,
            )
            print(_format_transcript(gateway.handle(command).events))
            return 0
        if args.command == "pause":
            command = _command(gateway, session_id=args.session_id, kind=CommandKind.PAUSE)
            print(_format_transcript(gateway.handle(command).events))
            return 0
        if args.command == "resume":
            command = _command(gateway, session_id=args.session_id, kind=CommandKind.RESUME)
            print(_format_transcript(gateway.handle(command).events))
            return 0
        if args.command == "replay":
            return _handle_replay(gateway, session_id=args.session_id)
        if args.command == "audit" and args.audit_command == "export":
            return _handle_audit_export(
                gateway,
                session_id=args.session_id,
                output=args.output,
            )
        if args.command == "reviewer" and args.reviewer_command == "packet":
            return _handle_reviewer_packet(
                workspace=args.workspace,
                session_id=args.session_id,
                fixture_root=args.fixture_root,
                output=args.output,
            )
        parser.print_help()
        return 0
    finally:
        gateway.stop()


def _handle_detect(gateway: SessionGateway, *, workspace: Path, session_id: str) -> int:
    command = _command(gateway, session_id=session_id, kind=CommandKind.DETECT)
    result = gateway.handle(command)
    detection = next(
        (
            event
            for event in result.events
            if _event_kind(event) == EventKind.DETECTION_PROPOSED.value
        ),
        None,
    )
    if detection is None:
        print("No detection event recorded.")
        return 1
    print(_format_detection(detection, workspace=workspace))
    return 0


def _handle_replay(gateway: SessionGateway, *, session_id: str) -> int:
    events = gateway.replay_events(session_id=session_id)
    if not events:
        print("No session events recorded.")
        return 0
    print(_format_transcript(events))
    return 0


def _handle_audit_export(
    gateway: SessionGateway,
    *,
    session_id: str,
    output: Path | None,
) -> int:
    command = _command(gateway, session_id=session_id, kind=CommandKind.AUDIT_EXPORT)
    events = gateway.handle(command).events
    if output is not None:
        export_event = events[-1]
        export_path = Path(str(export_event.payload["path"]))
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(export_path, output)
    print(_format_transcript(events))
    return 0


def _handle_reviewer_packet(
    *,
    workspace: Path,
    session_id: str,
    fixture_root: Path,
    output: Path,
) -> int:
    packet = ReviewerPacketGenerator(
        repository_root=Path(),
        session_workspace=workspace,
        session_id=session_id,
        fixture_root=fixture_root,
        output_dir=output,
    ).generate()
    print(f"Reviewer packet: {packet.index_path}")
    for path in packet.files:
        print(f"  - {path}")
    return 0


def _interactive_chat(gateway: SessionGateway, *, session_id: str) -> int:
    print("Heartwood chat. Use /quit, /pause, /resume, /replay, /audit-export, /approve, or /deny.")
    while True:
        try:
            line = input("heartwood> ").strip()
        except EOFError:
            print()
            return 0
        if line in {"", "/quit", "/exit"}:
            return 0
        if line.startswith("/"):
            _handle_chat_directive(gateway, session_id=session_id, line=line)
            continue
        command = _command(
            gateway,
            session_id=session_id,
            kind=CommandKind.CHAT,
            payload={"prompt": line},
        )
        print(_format_transcript(gateway.handle(command).events))


def _handle_chat_directive(gateway: SessionGateway, *, session_id: str, line: str) -> None:
    try:
        parts = shlex.split(line)
    except ValueError:
        print("Invalid directive syntax.")
        return
    directive = parts[0]
    if directive == "/pause":
        command = _command(gateway, session_id=session_id, kind=CommandKind.PAUSE)
        print(_format_transcript(gateway.handle(command).events))
    elif directive == "/resume":
        command = _command(gateway, session_id=session_id, kind=CommandKind.RESUME)
        print(_format_transcript(gateway.handle(command).events))
    elif directive == "/replay":
        _handle_replay(gateway, session_id=session_id)
    elif directive == "/audit-export":
        _handle_audit_export(gateway, session_id=session_id, output=None)
    elif directive in {"/approve", "/deny"} and len(parts) >= 3:
        kind = CommandKind.APPROVE if directive == "/approve" else CommandKind.DENY
        command = _command(
            gateway,
            session_id=session_id,
            kind=kind,
            payload={"target_type": parts[1], "target_id": parts[2]},
        )
        print(_format_transcript(gateway.handle(command).events))
    else:
        print(f"Unknown directive: {directive}")


def _command(
    gateway: SessionGateway,
    *,
    session_id: str,
    kind: CommandKind,
    payload: dict[str, JsonValue] | None = None,
) -> SessionCommand:
    sequence = len(gateway.replay_events(session_id=session_id))
    return SessionCommand(
        command_id=f"{session_id}-{kind.value}-{sequence:06d}",
        session_id=session_id,
        kind=kind,
        actor_id="human",
        created_at=_utc_now(),
        payload={} if payload is None else payload,
    )


def _event_kind(event: SessionEvent) -> str:
    return str(event.kind)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
