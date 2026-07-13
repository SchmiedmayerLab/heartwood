# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The ``heartwood`` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import uvicorn

from heartwood.cli._interactive import (
    InteractionResult,
    InteractiveSession,
    command_help,
    pending_actions,
)
from heartwood.cli._launch import LaunchOptions, run_launch
from heartwood.compliance import ReviewerPacketGenerator
from heartwood.gateway import (
    ActionSettingsError,
    DeploymentReadiness,
    GatewayAsgiApp,
    ModelArtifactError,
    ModelCatalogError,
    ModelProfile,
    ModelSettingsError,
    ModelSnapshotError,
    SessionGateway,
    SkillSettingsError,
    action_settings_path,
    inspect_deployment,
    model_settings_path,
    persist_deployment_profile,
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

__version__ = os.environ.get("HEARTWOOD_VERSION", "0.1.0")

_PROG = "heartwood"
_DEFAULT_WORKSPACE = Path(".heartwood") / "sessions"
_DEFAULT_FIXTURE_ROOT = Path("fixtures") / "synthetic"
_DEFAULT_WEB_ROOT = Path("packages") / "webui" / "dist"
_ACTION_MODE_ARGUMENTS = {
    "ask-every-time": "always-confirm",
    "auto-approve-low-risk": "confirm-risky",
}


def _default_workspace() -> Path:
    configured = os.environ.get("HEARTWOOD_WORKSPACE")
    if configured:
        return Path(configured)
    home = os.environ.get("HEARTWOOD_HOME")
    return Path(home) / "sessions" if home else _DEFAULT_WORKSPACE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Auditable agentic coding for sensitive biomedical research data.",
    )
    parser.add_argument("--version", action="version", version=f"{_PROG} {__version__}")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=_default_workspace(),
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
    chat.add_argument(
        "--plain",
        action="store_true",
        help="Use the line-oriented interface for basic terminals and automation.",
    )
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
    doctor = subparsers.add_parser("doctor", help="Inspect environment and setup readiness.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable diagnostics.")
    setup = subparsers.add_parser("setup", help="Configure a model route and conservative policy.")
    setup.add_argument(
        "--model-source",
        choices=("local", "stanford-ai-api-gateway"),
        help="Model service to configure.",
    )
    setup.add_argument("--model-id", help="Exact model identifier reported by the service.")
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="Require explicit inputs and do not prompt.",
    )
    setup.add_argument("--yes", action="store_true", help="Confirm the displayed configuration.")

    launch = subparsers.add_parser(
        "launch", help="Prepare platform compute and open an interactive Heartwood session."
    )
    launch.add_argument("--model-root", type=Path, help="Verified local-model snapshot directory.")
    launch.add_argument("--state-root", type=Path, help="Persistent Heartwood state root.")
    launch.add_argument("--environment-root", type=Path, help="Native runtime environment root.")
    launch.add_argument("--vllm-executable", type=Path, help="Explicit vLLM executable.")
    launch.add_argument("--model-id", default="heartwood-local-model")
    launch.add_argument(
        "--partition",
        help="Slurm GPU partition; by default Heartwood selects the available default.",
    )
    launch.add_argument("--gpus", type=int, default=1)
    launch.add_argument("--cpus", type=int, default=8)
    launch.add_argument("--memory", default="64G")
    launch.add_argument("--time", dest="time_limit", default="02:00:00")
    launch.add_argument("--startup-timeout", type=int, default=600)
    launch.add_argument("--dry-run", action="store_true")
    launch.add_argument("--no-allocate", action="store_true")
    launch.add_argument(
        "--yes-request-allocation",
        action="store_true",
        help="Confirm the displayed scheduler request without an interactive prompt.",
    )
    launch.add_argument("--inside-allocation", action="store_true", help=argparse.SUPPRESS)
    launch.add_argument("--plain", action="store_true", help="Open the line-oriented chat.")

    allow = subparsers.add_parser(
        "allow",
        aliases=["approve"],
        help="Allow the complete pending OpenHands action set once.",
    )
    allow.add_argument(
        "tool_call_id",
        nargs="?",
        help="Optional member id for automation compatibility.",
    )
    reject = subparsers.add_parser(
        "reject",
        aliases=["deny"],
        help="Reject the complete pending OpenHands action set.",
    )
    reject.add_argument(
        "tool_call_id",
        nargs="?",
        help="Optional member id for automation compatibility.",
    )
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
        "download", help="Download and verify a reviewed local model."
    )
    download.add_argument("model_id")
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
    if args.command == "doctor":
        return _handle_doctor(workspace=args.workspace, as_json=args.json)
    if args.command == "setup":
        return _handle_setup(parser, args)
    if args.command == "launch":
        if args.gpus < 1 or args.cpus < 1 or args.startup_timeout < 1:
            parser.error("--gpus, --cpus, and --startup-timeout must be positive")
        state_root = args.state_root or args.workspace.parent
        if args.workspace.parent != state_root:
            parser.error("--state-root must be the parent of --workspace")
        return run_launch(
            LaunchOptions(
                workspace=args.workspace,
                session_id=args.session_id,
                model_root=args.model_root,
                state_root=state_root,
                environment_root=args.environment_root,
                vllm_executable=args.vllm_executable,
                model_id=args.model_id,
                partition=args.partition,
                gpus=args.gpus,
                cpus=args.cpus,
                memory=args.memory,
                time_limit=args.time_limit,
                dry_run=args.dry_run,
                no_allocate=args.no_allocate,
                yes_request_allocation=args.yes_request_allocation,
                inside_allocation=args.inside_allocation,
                plain=args.plain,
                startup_timeout=args.startup_timeout,
            )
        )
    if args.command is None and sys.stdin.isatty():
        readiness = inspect_deployment(args.workspace)
        if readiness.state == "setup-required":
            print("Heartwood needs a model route before the first conversation.\n")
            return _handle_setup(parser, args)
        if readiness.state == "recovery-required":
            print(_format_readiness(readiness))
            print("\nResolve the failed checks, then run `heartwood doctor` again.")
            return 1

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
            return _interactive_chat(
                gateway,
                session_id=args.session_id,
                plain=getattr(args, "plain", False),
            )
        if args.command == "run":
            return _submit_task(
                gateway,
                session_id=args.session_id,
                prompt=args.prompt,
                kind=CommandKind.RUN,
            )
        if args.command in {"allow", "approve", "reject", "deny"}:
            directive = "/allow" if args.command in {"allow", "approve"} else "/reject"
            if args.tool_call_id:
                directive = f"{directive} {shlex.quote(args.tool_call_id)}"
            result = InteractiveSession(gateway, session_id=args.session_id).submit(directive)
            if result.message:
                print(result.message)
            if result.events:
                print(_format_transcript(result.events))
            return 1 if result.failed else 0
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


def _handle_doctor(*, workspace: Path, as_json: bool) -> int:
    readiness = inspect_deployment(workspace)
    print(json.dumps(readiness.safe_dict(), indent=2) if as_json else _format_readiness(readiness))
    return 1 if readiness.state == "recovery-required" else 0


def _format_readiness(readiness: DeploymentReadiness) -> str:
    lines = [
        "Heartwood environment",
        f"Platform: {readiness.platform_id}",
        f"State: {readiness.state}",
        "",
    ]
    markers = {"pass": "OK", "warning": "NOTE", "fail": "FAIL"}
    for check in readiness.checks:
        lines.append(f"[{markers[check.status]}] {check.summary}")
    return "\n".join(lines)


def _handle_setup(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    readiness = inspect_deployment(args.workspace)
    if readiness.state == "recovery-required":
        print(_format_readiness(readiness))
        print("\nSetup cannot continue until failed environment checks are resolved.")
        return 1
    source = getattr(args, "model_source", None)
    non_interactive = bool(getattr(args, "non_interactive", False))
    confirmed = bool(getattr(args, "yes", False))
    model_id = getattr(args, "model_id", None)
    if source is None:
        if non_interactive:
            parser.error("--model-source is required with --non-interactive")
        print(_format_readiness(readiness))
        print("\nModel access:\n  1. Local model service\n  2. Stanford AI API Gateway")
        try:
            choice = input("Select [1-2]: ").strip()
        except EOFError:
            print("\nSetup cancelled because input closed.")
            return 1
        source = "stanford-ai-api-gateway" if choice == "2" else "local"
    if non_interactive and model_id is None:
        parser.error("--model-id is required with --non-interactive")
    print("\nConfiguration")
    print(f"  Platform: {readiness.platform_id}")
    print(f"  Model source: {source}")
    print("  Action confirmation: Ask Every Time")
    if not confirmed:
        if non_interactive:
            parser.error("--yes is required with --non-interactive")
        try:
            confirmed = input("Apply this non-secret configuration? [y/N]: ").strip().lower() == "y"
        except EOFError:
            print("\nSetup cancelled because input closed.")
            return 1
    if not confirmed:
        print("Setup cancelled.")
        return 1
    model_source = cast(Literal["local", "stanford-ai-api-gateway"], source)
    snapshot = _snapshot_setup_files(args.workspace)
    try:
        persist_deployment_profile(args.workspace, model_source=model_source)
        gateway = SessionGateway(workspace=args.workspace)
        gateway.start()
        try:
            gateway.select_action_confirmation_mode("always-confirm")
            connection_id = "local" if source == "local" else "stanford-ai-api-gateway"
            catalog = gateway.discover_models(connection_id, refresh=True)
            models = catalog.get("models", [])
            if not isinstance(models, list):
                raise ModelCatalogError("the selected model service returned an invalid catalog")
            available = [
                item.get("model_id")
                for item in models
                if isinstance(item, dict) and item.get("availability") != "unsupported"
            ]
            if model_id is None:
                if not available:
                    raise ModelCatalogError("the selected model service reported no usable models")
                print("\nAvailable models:")
                for index, item in enumerate(available, start=1):
                    print(f"  {index}. {item}")
                try:
                    selected = input("Select a model by number or identifier: ").strip()
                except EOFError as error:
                    raise ModelCatalogError(
                        "model selection was cancelled because input closed"
                    ) from error
                if selected.isdigit() and 1 <= int(selected) <= len(available):
                    model_id = str(available[int(selected) - 1])
                else:
                    model_id = selected
            gateway.connect_model(connection_id, model_id)
        finally:
            gateway.stop()
    except (ActionSettingsError, ModelCatalogError, ModelSettingsError) as error:
        _restore_setup_files(snapshot)
        print(f"Setup could not validate the model route: {error}")
        return 1
    except BaseException:
        _restore_setup_files(snapshot)
        raise
    print("Setup complete. Run `heartwood` to start the conversation.")
    return 0


def _snapshot_setup_files(workspace: Path) -> dict[Path, tuple[bytes, int] | None]:
    state_root = workspace.parent
    paths = {
        state_root / "setup.json",
        state_root / "policy.json",
        state_root / "model-connections.json",
        model_settings_path(workspace),
        action_settings_path(workspace),
    }
    snapshot: dict[Path, tuple[bytes, int] | None] = {}
    for path in paths:
        snapshot[path] = (
            (path.read_bytes(), path.stat().st_mode & 0o777) if path.is_file() else None
        )
    return snapshot


def _restore_setup_files(snapshot: dict[Path, tuple[bytes, int] | None]) -> None:
    for path, previous in snapshot.items():
        if previous is None:
            path.unlink(missing_ok=True)
            continue
        contents, mode = previous
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(contents)
            temporary_path.chmod(mode)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


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
            path = gateway.download_local_model_now(
                args.model_id,
                cache_dir=args.cache,
            )
            print(f"Local model: {path}")
            return 0
    except (ModelArtifactError, ModelCatalogError, ModelSettingsError, ModelSnapshotError) as error:
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
    snapshots = catalog.get("snapshots", [])
    if isinstance(snapshots, list):
        for item in snapshots:
            if not isinstance(item, dict):
                continue
            size = item.get("expected_size_bytes")
            size_gib = float(size) / (1024**3) if isinstance(size, int | float) else 0
            lines.append(f"{item.get('snapshot_id')}  {size_gib:.2f} GiB")
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


def _interactive_chat(gateway: SessionGateway, *, session_id: str, plain: bool = False) -> int:
    session = InteractiveSession(gateway, session_id=session_id)
    if not plain and _supports_full_screen_terminal():
        from heartwood.cli._tui import run_terminal

        return run_terminal(session, format_events=_format_tui_event_lines)
    print(f"Heartwood agent. Commands: {command_help()}.")
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
        directive = line.split(maxsplit=1)[0]
        result = (
            _submit_with_progress(session, line)
            if not line.startswith("/") or directive in {"/allow", "/resume"}
            else session.submit(line)
        )
        if result.exit_requested:
            return 0
        if result.message:
            print(result.message)
        if result.events:
            print(_format_transcript(result.events, live=not result.replace_transcript))


def _submit_with_progress(
    session: InteractiveSession,
    line: str,
    *,
    update_interval: float = 15,
) -> InteractionResult:
    """Submit one blocking line-mode turn while reporting honest elapsed time."""
    stopped = threading.Event()
    started = time.monotonic()

    def report_progress() -> None:
        while not stopped.wait(update_interval):
            elapsed = int(time.monotonic() - started)
            print(f"Still working ({elapsed} seconds elapsed)...", flush=True)

    print("Working; local models may take several minutes...", flush=True)
    reporter = threading.Thread(
        target=report_progress,
        name="heartwood-line-progress",
        daemon=True,
    )
    reporter.start()
    try:
        return session.submit(line)
    finally:
        stopped.set()
        reporter.join()


def _supports_full_screen_terminal() -> bool:
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "").lower() not in {"", "dumb"}
    )


def _format_transcript(events: Sequence[SessionEvent], *, live: bool = False) -> str:
    return "\n".join(_format_event_lines(events, live=live))


def _format_event_lines(
    events: Sequence[SessionEvent],
    *,
    live: bool = False,
    include_pending_review: bool = True,
) -> tuple[str, ...]:
    pending = pending_actions(list(events))
    pending_ids = {action.tool_call_id for action in pending}
    lines: list[str] = []
    event_index = 0
    while event_index < len(events):
        event = events[event_index]
        kind = _event_kind(event)
        if live and kind == EventKind.USER_MESSAGE_RECORDED.value:
            event_index += 1
            continue
        if (
            kind == EventKind.TOOL_CALL_PROPOSED.value
            and event.payload.get("tool_call_id") in pending_ids
            and include_pending_review
        ):
            event_index += 1
            continue
        if kind == EventKind.CONFIRMATION_RESOLVED.value:
            resolved = [event]
            decision = str(event.payload.get("decision", "resolved"))
            event_index += 1
            while event_index < len(events):
                sibling = events[event_index]
                if (
                    _event_kind(sibling) != EventKind.CONFIRMATION_RESOLVED.value
                    or str(sibling.payload.get("decision", "resolved")) != decision
                ):
                    break
                resolved.append(sibling)
                event_index += 1
            sequence = (
                f"[{resolved[0].sequence:03d}]"
                if len(resolved) == 1
                else f"[{resolved[0].sequence:03d}-{resolved[-1].sequence:03d}]"
            )
            label = "action" if len(resolved) == 1 else "actions"
            lines.append(f"{sequence} Action set {decision} ({len(resolved)} {label})")
            continue
        if line := _format_event(event):
            lines.append(line)
        event_index += 1
    if pending and include_pending_review:
        label = "action" if len(pending) == 1 else "actions"
        lines.append(f"Review {len(pending)} {label} as one OpenHands action set:")
        for index, action in enumerate(pending, 1):
            lines.append(
                f"  {index}. {action.summary} [tool={action.tool_name}, risk={action.risk}]"
            )
        lines.extend(("Allow all once: /allow", "Reject all: /reject"))
    return tuple(lines)


def _format_tui_event_lines(events: Sequence[SessionEvent]) -> tuple[str, ...]:
    """Format durable transcript lines while the TUI owns pending-action controls."""
    return _format_event_lines(events, include_pending_review=False)


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
        return ""
    if kind == EventKind.AGENT_MESSAGE_EMITTED.value:
        return f"{prefix} Agent: {event.payload.get('content', '')}"
    if kind == EventKind.TOOL_CALL_PROPOSED.value:
        return (
            f"{prefix} Action: {event.payload.get('summary', event.payload.get('tool_name', ''))} "
            f"(risk={event.payload.get('risk', 'unknown')})"
        )
    if kind == EventKind.CONFIRMATION_REQUESTED.value:
        return ""
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
