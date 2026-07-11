# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The ``heartwood`` command-line interface."""

from __future__ import annotations

import argparse
import shlex
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from heartwood.compliance import ReviewerPacketGenerator
from heartwood.gateway import (
    ActionSettingsError,
    GatewayAsgiApp,
    ModelArtifactError,
    ModelCatalogError,
    ModelProfile,
    ModelSettingsError,
    SessionGateway,
    SkillSettingsError,
)
from heartwood.session import (
    CommandKind,
    EventKind,
    JsonValue,
    SessionCommand,
    SessionEvent,
    validate_session_id,
)

__all__ = ["__version__", "main"]

__version__ = "0.0.0"

_PROG = "heartwood"
_DEFAULT_WORKSPACE = Path(".heartwood") / "sessions"
_DEFAULT_FIXTURE_ROOT = Path("fixtures") / "synthetic"
_DEFAULT_WEB_ROOT = Path("packages") / "webui" / "dist"
_ACTION_MODE_ARGUMENTS = {
    "ask-every-time": "always-confirm",
    "auto-approve-low-risk": "confirm-risky",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Auditable agentic coding for sensitive biomedical research data.",
    )
    parser.add_argument("--version", action="version", version=f"{_PROG} {__version__}")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=_DEFAULT_WORKSPACE,
        help="Directory for local session state and model settings.",
    )
    parser.add_argument(
        "--session-id",
        default="session-local",
        type=_session_id_argument,
        help="Session identifier.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    chat = subparsers.add_parser(
        "chat",
        aliases=["agent"],
        help="Open the coding-agent conversation or submit one task.",
    )
    chat.add_argument("--prompt", "-p", help="Submit one task instead of opening the prompt loop.")
    run = subparsers.add_parser(
        "run",
        help="Compatibility alias for one coding-agent task.",
    )
    run.add_argument(
        "prompt",
        nargs="?",
        default="build the synthetic target-condition cohort and report aggregate quality checks",
    )
    subparsers.add_parser("detect", help="Detect the platform and dataset without running code.")

    allow = subparsers.add_parser(
        "allow",
        aliases=["approve"],
        help="Allow the current pending action once.",
    )
    allow.add_argument("tool_call_id", help="Pending action id shown in the transcript.")
    reject = subparsers.add_parser(
        "reject",
        aliases=["deny"],
        help="Reject the current pending action.",
    )
    reject.add_argument("tool_call_id", help="Pending action id shown in the transcript.")
    subparsers.add_parser("pause", help="Pause the current session.")
    subparsers.add_parser("resume", help="Resume the current session.")
    subparsers.add_parser("replay", help="Replay the persisted session event stream.")

    actions = subparsers.add_parser("actions", help="Configure action confirmation.")
    action_subparsers = actions.add_subparsers(dest="actions_command", metavar="<actions-command>")
    action_set = action_subparsers.add_parser(
        "set", help="Select an action-confirmation mode allowed by platform policy."
    )
    action_set.add_argument("mode", choices=tuple(_ACTION_MODE_ARGUMENTS))

    models = subparsers.add_parser(
        "models", help="Choose a model connection or manage advanced profiles."
    )
    model_subparsers = models.add_subparsers(dest="models_command", metavar="<models-command>")
    model_subparsers.add_parser("list", help="List connections and the active model profile.")
    refresh_models = model_subparsers.add_parser(
        "refresh", help="List every model currently exposed by a connection."
    )
    refresh_models.add_argument("connection_id")
    refresh_models.add_argument("--base-url", help="Server URL for a Custom API connection.")
    connect_model = model_subparsers.add_parser(
        "connect", help="Discover, select, and activate one model."
    )
    connect_model.add_argument("connection_id")
    connect_model.add_argument("model_id")
    connect_model.add_argument("--base-url", help="Server URL for a Custom API connection.")
    connect_model.add_argument(
        "--manual",
        action="store_true",
        help="Use a Custom API model identifier when its server cannot list models.",
    )
    model_subparsers.add_parser("artifacts", help="List reviewed local-model artifacts.")
    add = model_subparsers.add_parser(
        "add", help="Advanced: add or update a non-secret model profile."
    )
    add.add_argument("profile_id")
    add.add_argument(
        "--model", required=True, help="LiteLLM model id, including its provider prefix."
    )
    add.add_argument(
        "--policy-endpoint", required=True, help="Exact endpoint authorized by policy."
    )
    add.add_argument("--base-url", help="Custom provider or local OpenAI-compatible base URL.")
    add.add_argument(
        "--credential-kind",
        choices=("environment", "file", "managed-identity", "none"),
        default="environment",
    )
    add.add_argument("--api-key-env", help="Environment variable containing the API key.")
    add.add_argument("--api-key-file", help="Absolute mounted file containing the API key.")
    add.add_argument("--api-version")
    add.add_argument("--aws-region-name")
    add.add_argument("--aws-profile-name")
    add.add_argument(
        "--capability-tier",
        choices=("autonomous", "supervised", "experimental"),
        default="supervised",
    )
    add.add_argument("--description")
    add.add_argument("--select", action="store_true", help="Select this profile after saving it.")
    select = model_subparsers.add_parser("select", help="Advanced: select a saved profile.")
    select.add_argument("profile_id")
    validate = model_subparsers.add_parser(
        "validate", help="Check credentials and platform route authorization."
    )
    validate.add_argument("profile_id", nargs="?")
    remove = model_subparsers.add_parser("remove", help="Remove a profile.")
    remove.add_argument("profile_id")
    download = model_subparsers.add_parser(
        "download", help="Download and verify a reviewed Hugging Face artifact."
    )
    download.add_argument("artifact_id")
    download.add_argument("--cache", type=Path, help="Mounted model cache directory.")

    skills = subparsers.add_parser("skills", help="Inspect bundled Skills and extensions.")
    skill_subparsers = skills.add_subparsers(dest="skills_command", metavar="<skills-command>")
    skill_subparsers.add_parser("list", help="List bundled and installed Skills.")
    inspect = skill_subparsers.add_parser(
        "inspect", help="Verify and summarize a mounted Skill source."
    )
    inspect.add_argument("source", type=Path)
    install = skill_subparsers.add_parser(
        "install", help="Install a mounted Skill after explicit review."
    )
    install.add_argument("source", type=Path)
    install.add_argument(
        "--approve",
        action="store_true",
        help="Record approval of the displayed permissions and install the Skill.",
    )
    remove_skill = skill_subparsers.add_parser("remove", help="Remove an installed extension.")
    remove_skill.add_argument("name")

    audit = subparsers.add_parser("audit", help="Audit-log operations.")
    audit_subparsers = audit.add_subparsers(dest="audit_command", metavar="<audit-command>")
    audit_export = audit_subparsers.add_parser("export", help="Export scrubbed audit JSONL.")
    audit_export.add_argument("--output", type=Path, help="Optional copy destination.")

    reviewer = subparsers.add_parser("reviewer", help="Reviewer artifact operations.")
    reviewer_subparsers = reviewer.add_subparsers(
        dest="reviewer_command", metavar="<reviewer-command>"
    )
    packet = reviewer_subparsers.add_parser(
        "packet", help="Generate the synthetic reviewer artifact set."
    )
    packet.add_argument("--output", type=Path, default=Path("compliance") / "reviewer-packet")
    packet.add_argument("--fixture-root", type=Path, default=_DEFAULT_FIXTURE_ROOT)

    serve = subparsers.add_parser("serve", help="Serve the gateway and packaged web UI.")
    serve.add_argument("--host", default="127.0.0.1", help="Gateway bind host.")
    serve.add_argument("--port", type=int, default=8767, help="Gateway bind port.")
    serve.add_argument("--web-root", type=Path, default=_DEFAULT_WEB_ROOT)
    serve.add_argument("--base-path", default="/", help="Base path behind a notebook proxy.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``heartwood`` and return a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None and not sys.stdin.isatty():
        parser.print_help()
        return 0
    if args.command == "serve":
        return _handle_serve(
            workspace=args.workspace,
            host=args.host,
            port=args.port,
            web_root=args.web_root,
            base_path=args.base_path,
        )
    if args.command == "reviewer" and args.reviewer_command == "packet":
        return _handle_reviewer_packet(
            workspace=args.workspace,
            session_id=args.session_id,
            fixture_root=args.fixture_root,
            output=args.output,
        )

    gateway = SessionGateway(workspace=args.workspace)
    gateway.start()
    try:
        if args.command == "models":
            return _handle_models(parser, gateway, args)
        if args.command == "actions":
            return _handle_actions(parser, gateway, args)
        if args.command == "skills":
            return _handle_skills(parser, gateway, args)
        if args.command == "detect":
            return _handle_detect(gateway, workspace=args.workspace, session_id=args.session_id)
        if args.command in {None, "chat", "agent"}:
            if getattr(args, "prompt", None) is not None:
                return _submit_task(gateway, session_id=args.session_id, prompt=args.prompt)
            return _interactive_chat(gateway, session_id=args.session_id)
        if args.command == "run":
            return _submit_task(
                gateway,
                session_id=args.session_id,
                prompt=args.prompt,
                kind=CommandKind.RUN,
            )
        if args.command in {"allow", "approve", "reject", "deny"}:
            kind = CommandKind.APPROVE if args.command in {"allow", "approve"} else CommandKind.DENY
            command = _command(
                gateway,
                session_id=args.session_id,
                kind=kind,
                payload={"target_type": "tool-call", "target_id": args.tool_call_id},
            )
            events = gateway.handle(command).events
            print(_format_transcript(events))
            return _event_exit_code(events)
        if args.command == "pause":
            return _submit_simple(gateway, session_id=args.session_id, kind=CommandKind.PAUSE)
        if args.command == "resume":
            return _submit_simple(gateway, session_id=args.session_id, kind=CommandKind.RESUME)
        if args.command == "replay":
            return _handle_replay(gateway, session_id=args.session_id)
        if args.command == "audit" and args.audit_command == "export":
            return _handle_audit_export(gateway, session_id=args.session_id, output=args.output)
        parser.print_help()
        return 0
    finally:
        gateway.stop()


def _handle_models(
    parser: argparse.ArgumentParser,
    gateway: SessionGateway,
    args: argparse.Namespace,
) -> int:
    command = getattr(args, "models_command", None)
    try:
        if command == "list":
            print(_format_model_settings(gateway.model_settings()))
            return 0
        if command == "artifacts":
            print(_format_model_artifacts(gateway.model_artifacts()))
            return 0
        if command == "refresh":
            catalog = gateway.discover_models(
                args.connection_id,
                base_url=args.base_url,
                refresh=True,
            )
            print(_format_model_catalog(catalog))
            return 0
        if command == "connect":
            if not args.manual:
                gateway.discover_models(
                    args.connection_id,
                    base_url=args.base_url,
                    refresh=True,
                )
            settings = gateway.connect_model(
                args.connection_id,
                args.model_id,
                base_url=args.base_url,
                manual=args.manual,
            )
            print(_format_model_settings(settings))
            print()
            print(_format_model_validation(gateway.validate_model_profile()))
            return 0
        if command == "add":
            profile = ModelProfile(
                profile_id=args.profile_id,
                model=args.model,
                policy_endpoint=args.policy_endpoint,
                capability_tier=args.capability_tier,
                base_url=args.base_url,
                credential_kind=args.credential_kind,
                api_key_env=args.api_key_env,
                api_key_file=args.api_key_file,
                api_version=args.api_version,
                aws_region_name=args.aws_region_name,
                aws_profile_name=args.aws_profile_name,
                description=args.description,
            )
            settings = gateway.save_model_profile(profile)
            if args.select:
                settings = gateway.select_model_profile(profile.profile_id)
            print(_format_model_settings(settings))
            return 0
        if command == "select":
            print(_format_model_settings(gateway.select_model_profile(args.profile_id)))
            return 0
        if command == "validate":
            print(_format_model_validation(gateway.validate_model_profile(args.profile_id)))
            return 0
        if command == "remove":
            print(_format_model_settings(gateway.remove_model_profile(args.profile_id)))
            return 0
        if command == "download":
            path = gateway.download_model_artifact_now(
                args.artifact_id,
                cache_dir=args.cache,
            )
            print(f"Model artifact: {path}")
            return 0
    except (ModelArtifactError, ModelCatalogError, ModelSettingsError) as error:
        parser.error(str(error))
    parser.parse_args(["models", "--help"])
    return 0


def _handle_actions(
    parser: argparse.ArgumentParser,
    gateway: SessionGateway,
    args: argparse.Namespace,
) -> int:
    """Show or update the shared OpenHands action-confirmation mode."""
    try:
        if getattr(args, "actions_command", None) == "set":
            settings = gateway.select_action_confirmation_mode(_ACTION_MODE_ARGUMENTS[args.mode])
        else:
            settings = gateway.action_settings()
    except ActionSettingsError as error:
        parser.error(str(error))
    print(_format_action_settings(settings))
    return 0


def _format_action_settings(settings: dict[str, object]) -> str:
    lines = ["Action confirmation", ""]
    selected = settings.get("confirmation_mode")
    modes = settings.get("modes", [])
    if isinstance(modes, list):
        for item in modes:
            if not isinstance(item, dict):
                continue
            marker = "*" if item.get("mode") == selected else " "
            availability = "" if item.get("allowed") else " (not allowed by policy)"
            lines.append(f"{marker} {item.get('label')}{availability}")
    return "\n".join(lines)


def _format_model_settings(settings: dict[str, object]) -> str:
    lines = ["Heartwood models", "", "Connections:"]
    connections = settings.get("connections", [])
    if isinstance(connections, list):
        for item in connections:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            status = item.get("credential_status", "unknown")
            lines.append(
                f"  {item.get('connection_id')}  {item.get('label')}  "
                f"source={source}  credentials={status}"
            )
    lines.extend(("", "Active and saved profiles:"))
    active = settings.get("active_profile")
    profiles = settings.get("profiles", [])
    if isinstance(profiles, list) and profiles:
        for item in profiles:
            if not isinstance(item, dict):
                continue
            marker = "*" if item.get("profile_id") == active else " "
            lines.append(
                f"{marker} {item.get('profile_id')}  {item.get('model')}  "
                f"credentials={item.get('credential_status', 'unknown')}"
            )
            lines.append(f"    policy endpoint: {item.get('policy_endpoint')}")
    else:
        lines.append("No model profiles configured.")
    return "\n".join(lines)


def _format_model_catalog(catalog: dict[str, object]) -> str:
    connection = catalog.get("connection", {})
    if not isinstance(connection, dict):
        return "Model catalog returned malformed connection metadata."
    connection_id = connection.get("connection_id")
    lines = [f"Models available from {connection.get('label')}", ""]
    models = catalog.get("models", [])
    if not isinstance(models, list) or not models:
        return "\n".join((*lines, "No models available."))
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("model_id")
        display_name = item.get("display_name")
        label = model_id if display_name in {None, model_id} else f"{display_name} ({model_id})"
        lines.append(f"  {label}  [{item.get('availability', 'unknown')}]")
        lines.append(f"    {item.get('reason', '')}")
    lines.extend(("", f"Select with: heartwood models connect {connection_id} <model-id>"))
    return "\n".join(lines)


def _format_model_validation(validation: dict[str, object]) -> str:
    profile = validation.get("profile", {})
    decision = validation.get("policy_decision", {})
    if not isinstance(profile, dict) or not isinstance(decision, dict):
        return "Model profile validation returned malformed data."
    return "\n".join(
        (
            f"Profile: {profile.get('profile_id')}",
            f"Model: {profile.get('model')}",
            f"Credentials: {validation.get('credential_status')}",
            f"Action confirmation: {validation.get('action_confirmation_mode')}",
            f"Policy: {decision.get('decision')} ({decision.get('reason')})",
        )
    )


def _format_model_artifacts(catalog: dict[str, object]) -> str:
    lines = ["Heartwood local-model artifacts", ""]
    artifacts = catalog.get("artifacts", [])
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            size = item.get("artifact_size_bytes")
            size_gib = float(size) / (1024**3) if isinstance(size, int | float) else 0
            lines.append(f"{item.get('artifact_id')}  {size_gib:.2f} GiB")
            lines.append(f"    {item.get('purpose')}")
    return "\n".join(lines)


def _handle_skills(
    parser: argparse.ArgumentParser,
    gateway: SessionGateway,
    args: argparse.Namespace,
) -> int:
    command = getattr(args, "skills_command", None)
    try:
        if command == "list":
            print(_format_skill_settings(gateway.skill_settings()))
            return 0
        if command == "inspect":
            print(_format_skill_summary(gateway.inspect_skill(args.source)))
            return 0
        if command == "install":
            if not args.approve:
                summary = gateway.inspect_skill(args.source)
                parser.error(
                    "installation approval is required; review with `heartwood skills inspect` "
                    f"and rerun with --approve\n{_format_skill_summary(summary)}"
                )
            print(_format_skill_settings(gateway.install_skill(args.source, approved=True)))
            return 0
        if command == "remove":
            print(_format_skill_settings(gateway.remove_skill(args.name)))
            return 0
    except SkillSettingsError as error:
        parser.error(str(error))
    parser.parse_args(["skills", "--help"])
    return 0


def _format_skill_settings(settings: dict[str, object]) -> str:
    lines = ["Heartwood Skills", ""]
    skills = settings.get("skills", [])
    if not isinstance(skills, list) or not skills:
        return "\n".join((*lines, "No Skills available."))
    for item in skills:
        if isinstance(item, dict):
            lines.append(
                f"{item.get('name')}  trust={item.get('trust_tier')}  source={item.get('source')}"
            )
            lines.append(f"    {item.get('description')}")
    return "\n".join(lines)


def _format_skill_summary(summary: dict[str, object]) -> str:
    tools = summary.get("declared_tools", [])
    tool_text = ", ".join(str(tool) for tool in tools) if isinstance(tools, list) else ""
    return "\n".join(
        (
            f"Skill: {summary.get('name')}",
            f"Trust: {summary.get('trust_tier')}",
            f"Tools: {tool_text}",
            f"Network: {'required' if summary.get('requires_network') else 'disabled'}",
            f"Permissions: {summary.get('approval_summary')}",
        )
    )


def _submit_task(
    gateway: SessionGateway,
    *,
    session_id: str,
    prompt: str,
    kind: CommandKind = CommandKind.CHAT,
) -> int:
    command = _command(
        gateway,
        session_id=session_id,
        kind=kind,
        payload={"prompt": prompt},
    )
    events = gateway.handle(command).events
    print(_format_transcript(events))
    return 1 if any(_event_kind(event) == EventKind.ERROR_RECORDED.value for event in events) else 0


def _submit_simple(gateway: SessionGateway, *, session_id: str, kind: CommandKind) -> int:
    events = gateway.handle(_command(gateway, session_id=session_id, kind=kind)).events
    print(_format_transcript(events))
    return _event_exit_code(events)


def _event_exit_code(events: Sequence[SessionEvent]) -> int:
    return 1 if any(_event_kind(event) == EventKind.ERROR_RECORDED.value for event in events) else 0


def _interactive_chat(gateway: SessionGateway, *, session_id: str) -> int:
    print(
        "Heartwood agent. Commands: /allow <id>, /reject <id>, /pause, /resume, "
        "/status, /replay, /audit-export, /exit."
    )
    while True:
        try:
            line = input("heartwood> ").strip()
        except EOFError:
            print()
            return 0
        if line in {"/quit", "/exit"}:
            return 0
        if not line:
            continue
        if line.startswith("/"):
            _handle_chat_directive(gateway, session_id=session_id, line=line)
            continue
        _submit_task(gateway, session_id=session_id, prompt=line)


def _handle_chat_directive(gateway: SessionGateway, *, session_id: str, line: str) -> None:
    try:
        parts = shlex.split(line)
    except ValueError:
        print("Invalid command syntax.")
        return
    directive = parts[0]
    if directive in {"/allow", "/reject"} and len(parts) == 2:
        kind = CommandKind.APPROVE if directive == "/allow" else CommandKind.DENY
        command = _command(
            gateway,
            session_id=session_id,
            kind=kind,
            payload={"target_type": "tool-call", "target_id": parts[1]},
        )
        print(_format_transcript(gateway.handle(command).events))
    elif directive == "/pause":
        _submit_simple(gateway, session_id=session_id, kind=CommandKind.PAUSE)
    elif directive == "/resume":
        _submit_simple(gateway, session_id=session_id, kind=CommandKind.RESUME)
    elif directive == "/status":
        try:
            print(_format_model_validation(gateway.validate_model_profile()))
        except ModelSettingsError as error:
            print(str(error))
    elif directive == "/replay":
        _handle_replay(gateway, session_id=session_id)
    elif directive == "/audit-export":
        _handle_audit_export(gateway, session_id=session_id, output=None)
    else:
        print(f"Unknown command: {directive}")


def _format_transcript(events: Sequence[SessionEvent]) -> str:
    return "\n".join(line for event in events if (line := _format_event(event)))


def _format_event(event: SessionEvent) -> str:
    kind = _event_kind(event)
    prefix = f"[{event.sequence:03d}]"
    if kind == EventKind.COMMAND_RECEIVED.value:
        return ""
    if kind == EventKind.DETECTION_PROPOSED.value:
        dataset = _mapping_payload(event.payload["dataset"], "dataset")
        platform = _mapping_payload(event.payload["platform"], "platform")
        return f"{prefix} Detected {platform['adapter_id']} / {dataset['dataset_type']}"
    if kind == EventKind.USER_MESSAGE_RECORDED.value:
        return f"{prefix} You: {event.payload.get('content', '')}"
    if kind == EventKind.MODEL_CALL_DECISION_RECORDED.value:
        decision = _mapping_payload(event.payload["decision"], "decision")
        return f"{prefix} Model route {decision['decision']}: {decision['endpoint']}"
    if kind == EventKind.AGENT_MESSAGE_EMITTED.value:
        return f"{prefix} Agent: {event.payload.get('content', '')}"
    if kind == EventKind.TOOL_CALL_PROPOSED.value:
        return (
            f"{prefix} Action: {event.payload.get('summary', event.payload.get('tool_name', ''))} "
            f"(risk={event.payload.get('risk', 'unknown')})"
        )
    if kind == EventKind.CONFIRMATION_REQUESTED.value:
        request = _mapping_payload(event.payload["request"], "request")
        return f"{prefix} Allow once or reject: {request['request_id']} ({request['tool_call_id']})"
    if kind == EventKind.CONFIRMATION_RESOLVED.value:
        return f"{prefix} Action {event.payload.get('decision', '')}"
    if kind == EventKind.TOOL_EXECUTION_RECORDED.value:
        return (
            f"{prefix} Tool {event.payload.get('tool_name', '')} "
            f"exit={event.payload.get('exit_code', '')}"
        )
    if kind == EventKind.SESSION_PAUSED.value:
        return f"{prefix} Session paused"
    if kind == EventKind.SESSION_RESUMED.value:
        return f"{prefix} Session resumed"
    if kind == EventKind.AUDIT_EXPORT_RECORDED.value:
        return f"{prefix} Audit export: {event.payload.get('path', '')}"
    if kind == EventKind.ERROR_RECORDED.value:
        return f"{prefix} Error: {event.payload.get('reason', '')}"
    return ""


def _handle_detect(gateway: SessionGateway, *, workspace: Path, session_id: str) -> int:
    result = gateway.handle(_command(gateway, session_id=session_id, kind=CommandKind.DETECT))
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
    platform = _mapping_payload(detection.payload["platform"], "platform")
    dataset = _mapping_payload(detection.payload["dataset"], "dataset")
    print("Heartwood environment detection")
    print(f"Session: {session_id}")
    print(f"State: {workspace}")
    print(f"Platform: {platform['adapter_id']} ({_float_payload(platform['confidence']):.2f})")
    print(f"Dataset: {dataset['dataset_type']} ({_float_payload(dataset['confidence']):.2f})")
    return 0


def _handle_replay(gateway: SessionGateway, *, session_id: str) -> int:
    events = gateway.replay_events(session_id=session_id)
    print(_format_transcript(events) if events else "No session events recorded.")
    return 0


def _handle_audit_export(
    gateway: SessionGateway,
    *,
    session_id: str,
    output: Path | None,
) -> int:
    events = gateway.handle(
        _command(gateway, session_id=session_id, kind=CommandKind.AUDIT_EXPORT)
    ).events
    if output is not None:
        export_path = Path(str(events[-1].payload["path"]))
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
    print(f"Reviewer artifacts: {packet.index_path}")
    for path in packet.files:
        print(f"  - {path}")
    return 0


def _handle_serve(
    *,
    workspace: Path,
    host: str,
    port: int,
    web_root: Path,
    base_path: str,
) -> int:
    if not web_root.exists():
        msg = f"web UI assets not found: {web_root}"
        raise SystemExit(msg)
    app = GatewayAsgiApp(
        SessionGateway(workspace=workspace),
        static_dir=web_root,
        static_base_path=base_path,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


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


def _mapping_payload(value: JsonValue, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        msg = f"expected {name} payload to be an object"
        raise TypeError(msg)
    return value


def _session_id_argument(value: str) -> str:
    try:
        return validate_session_id(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _float_payload(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = "expected a numeric payload"
        raise TypeError(msg)
    return float(value)


def _event_kind(event: SessionEvent) -> str:
    return str(event.kind)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
