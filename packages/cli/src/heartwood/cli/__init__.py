# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The ``heartwood`` command-line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from heartwood.adapters.platform import select_platform_adapter
from heartwood.cli._interactive import (
    InteractionActivity,
    InteractionResult,
    InteractiveSession,
    command_help,
    format_action_arguments,
    interaction_activity,
    pending_actions,
)
from heartwood.cli._launch import LaunchOptions, run_launch
from heartwood.compliance import ReviewerPacketGenerator
from heartwood.gateway import (
    BUILT_IN_MODEL_CONNECTIONS,
    MODEL_SOURCE_OPTIONS,
    ActionSettingsError,
    DeploymentReadiness,
    GatewayAsgiApp,
    ModelArtifactError,
    ModelCatalogError,
    ModelProfile,
    ModelRepositoryError,
    ModelSettingsError,
    ModelSnapshotError,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    SessionGateway,
    SkillSettingsError,
    has_authenticated_jupyter_proxy,
    inspect_deployment,
    jupyter_proxy_url,
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

__version__ = "0.2.0-beta.3"

_PROG = "heartwood"


def _bundled_path(relative: Path) -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.exists():
            return candidate
    return relative


def _bundled_repository_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "packages").is_dir():
            return parent
    msg = "Heartwood repository assets are unavailable in this installation"
    raise RuntimeError(msg)


_DEFAULT_FIXTURE_ROOT = _bundled_path(Path("fixtures") / "synthetic")
_DEFAULT_WEB_ROOT = _bundled_path(Path("packages") / "webui" / "dist")
_ACTION_MODE_ARGUMENTS = {
    "ask-every-time": "always-confirm",
    "auto-approve-low-risk": "confirm-risky",
}
_MODEL_SOURCE_IDS = tuple(option.source_id for option in MODEL_SOURCE_OPTIONS)
_MODEL_SOURCE_LABELS = {option.source_id: option.label for option in MODEL_SOURCE_OPTIONS}
_MODEL_DOWNLOAD_ACTIVITY = InteractionActivity(
    label="Downloading and verifying the model",
    waiting_label="Still downloading and verifying the model",
    guidance="Large models can take several minutes. Keep this process running.",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Auditable agentic coding for sensitive biomedical research data.",
    )
    parser.add_argument("--version", action="version", version=f"{_PROG} {__version__}")
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
        choices=_MODEL_SOURCE_IDS,
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
    launch.add_argument(
        "--web",
        action="store_true",
        help="Open the web interface while Heartwood supervises the local model.",
    )
    launch.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web bind host when --web is selected.",
    )
    launch.add_argument(
        "--port",
        type=int,
        default=8767,
        help="Web bind port when --web is selected.",
    )

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
    model_subparsers.add_parser("local", help="List available local model choices.")
    inspect_models = model_subparsers.add_parser(
        "inspect", help="Inspect supported models in a Hugging Face repository."
    )
    inspect_models.add_argument("repository", help="Hugging Face owner/model identifier.")
    inspect_models.add_argument("--revision", help="Branch, tag, or commit to inspect.")
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
        "download", help="Download a recommended or inspected Hugging Face model."
    )
    download.add_argument("model", help="Default model id or Hugging Face owner/model identifier.")
    download.add_argument(
        "--revision",
        help="Advanced: repository branch, tag, or commit for an owner/model identifier.",
    )

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
    project = ProjectContext.current()
    if args.command is None and not sys.stdin.isatty():
        parser.print_help()
        return 0
    if args.command == "serve":
        return _handle_serve(
            project=project,
            host=args.host,
            port=args.port,
            web_root=args.web_root,
            base_path=args.base_path,
        )
    if args.command == "reviewer" and args.reviewer_command == "packet":
        return _handle_reviewer_packet(
            project=project,
            session_id=args.session_id,
            fixture_root=args.fixture_root,
            output=args.output,
        )
    if args.command == "doctor":
        return _handle_doctor(project=project, as_json=args.json)
    if args.command == "setup":
        return _handle_setup(parser, args, project=project)
    if args.command == "launch":
        if args.gpus < 1 or args.cpus < 1 or args.startup_timeout < 1 or args.port < 1:
            parser.error("--gpus, --cpus, --startup-timeout, and --port must be positive")
        return run_launch(
            LaunchOptions(
                project=project,
                session_id=args.session_id,
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
                web=args.web,
                web_host=args.host,
                web_port=args.port,
                startup_timeout=args.startup_timeout,
            )
        )
    configured_gateway: SessionGateway | None = None
    if args.command is None and sys.stdin.isatty():
        readiness = inspect_deployment(project)
        if readiness.state == "setup-required":
            print("Heartwood needs a model route before the first conversation.\n")
            setup_code, configured_gateway = _configure_setup(parser, args, project=project)
            if setup_code != 0:
                return setup_code
            readiness = inspect_deployment(project)
        if readiness.state == "recovery-required":
            print(_format_readiness(readiness))
            print("\nResolve the failed checks, then run `heartwood doctor` again.")
            if configured_gateway is not None:
                configured_gateway.stop()
            return 1
        if readiness.state == "compute-required":
            print(_format_readiness(readiness))
            print("\nLocal inference is configured. Start it with `heartwood launch`.")
            if configured_gateway is not None:
                configured_gateway.stop()
            return 0

    gateway = configured_gateway or SessionGateway(project=project)
    gateway.start()
    try:
        if args.command == "models":
            return _handle_models(parser, gateway, args)
        if args.command == "actions":
            return _handle_actions(parser, gateway, args)
        if args.command == "skills":
            return _handle_skills(parser, gateway, args)
        if args.command == "detect":
            return _handle_detect(gateway, project=project, session_id=args.session_id)
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


def _handle_doctor(*, project: ProjectContext, as_json: bool) -> int:
    readiness = inspect_deployment(project)
    print(json.dumps(readiness.safe_dict(), indent=2) if as_json else _format_readiness(readiness))
    return 1 if readiness.state == "recovery-required" else 0


def _format_readiness(readiness: DeploymentReadiness) -> str:
    lines = [
        "Heartwood environment",
        f"Project: {readiness.project_root}",
        f"Heartwood data: {readiness.state_root}",
        f"Platform: {readiness.platform_id}",
        f"Readiness: {readiness.state}",
        "",
    ]
    markers = {"pass": "OK", "warning": "NOTE", "fail": "FAIL"}
    for check in readiness.checks:
        lines.append(f"[{markers[check.status]}] {check.summary}")
    return "\n".join(lines)


def _handle_setup(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    project: ProjectContext,
) -> int:
    code, gateway = _configure_setup(parser, args, project=project)
    if gateway is not None:
        gateway.stop()
    if code == 0:
        readiness = inspect_deployment(project)
        if readiness.state == "compute-required":
            print("Run `heartwood launch` to start the selected model and conversation.")
        else:
            print("Run `heartwood` to start the conversation.")
    return code


def _configure_setup(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    project: ProjectContext,
) -> tuple[int, SessionGateway | None]:
    readiness = inspect_deployment(project)
    if readiness.state == "recovery-required":
        print(_format_readiness(readiness))
        print("\nSetup cannot continue until failed environment checks are resolved.")
        return 1, None
    source = getattr(args, "model_source", None)
    non_interactive = bool(getattr(args, "non_interactive", False))
    confirmed = bool(getattr(args, "yes", False))
    model_id = getattr(args, "model_id", None)
    resume_existing = False
    resume_managed_local = False
    if project.config_path.is_file():
        adapter = select_platform_adapter(os.environ)
        config_store = ProjectConfigStore(
            project,
            ProjectConfig(
                platform_id=adapter.adapter_id,
                policy=adapter.default_policy_profile(),
            ),
        )
        existing = config_store.load()
        if source is None and existing.model_source is not None:
            try:
                existing_profile = existing.model_settings.profile()
            except ModelSettingsError:
                pass
            else:
                source = existing.model_source
                if source == "local" and existing.local_model is not None:
                    model_id = existing.local_model.artifact_id
                    resume_managed_local = True
                else:
                    source_option = next(
                        (item for item in MODEL_SOURCE_OPTIONS if item.source_id == source),
                        None,
                    )
                    connections = (
                        *BUILT_IN_MODEL_CONNECTIONS,
                        *existing.additional_connections,
                    )
                    connection = next(
                        (
                            item
                            for item in connections
                            if item.connection_id
                            == (source if source_option is None else source_option.connection_id)
                        ),
                        None,
                    )
                    model_id = (
                        existing_profile.model
                        if connection is None
                        else connection.provider_model_id(existing_profile.model)
                    )
                resume_existing = True
    if source is None:
        if non_interactive:
            parser.error("--model-source is required with --non-interactive")
        print(_format_readiness(readiness))
        print("\nModel access:")
        for index, source_id in enumerate(_MODEL_SOURCE_IDS, start=1):
            print(f"  {index}. {_MODEL_SOURCE_LABELS[source_id]}")
        try:
            choice = input(f"Select [1-{len(_MODEL_SOURCE_IDS)}]: ").strip()
        except EOFError:
            print("\nSetup cancelled because input closed.")
            return 1, None
        if not choice.isdigit() or not 1 <= int(choice) <= len(_MODEL_SOURCE_IDS):
            print("Setup cancelled because no valid model source was selected.")
            return 1, None
        source = _MODEL_SOURCE_IDS[int(choice) - 1]
    if non_interactive and model_id is None:
        parser.error("--model-id is required with --non-interactive")
    print("\nConfiguration")
    print(f"  Platform: {readiness.platform_id}")
    print(f"  Model source: {_MODEL_SOURCE_LABELS.get(source, source)}")
    print(
        "  Action confirmation: Existing project setting"
        if resume_existing
        else "  Action confirmation: Ask Every Time"
    )
    if not confirmed and not resume_existing:
        if non_interactive:
            parser.error("--yes is required with --non-interactive")
        try:
            confirmed = input("Apply this non-secret configuration? [y/N]: ").strip().lower() == "y"
        except EOFError:
            print("\nSetup cancelled because input closed.")
            return 1, None
    if not confirmed and not resume_existing:
        print("Setup cancelled.")
        return 1, None
    snapshot = _snapshot_setup_file(project)
    gateway: SessionGateway | None = None
    try:
        gateway = SessionGateway(project=project)
        if not resume_existing:
            gateway.configure_model_source(source)
        gateway.start()
        if not resume_existing:
            gateway.select_action_confirmation_mode("always-confirm")
        if source == "local":
            if not resume_managed_local:
                _configure_local_model(
                    gateway,
                    model_id=model_id,
                    non_interactive=non_interactive,
                )
            print("Setup complete.")
            return 0, gateway
        connection_id = "local" if source == "local" else source
        token = _prompt_for_provider_token(
            gateway,
            connection_id=connection_id,
            non_interactive=non_interactive,
        )
        catalog = gateway.discover_models(connection_id, token=token, refresh=True)
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
    except (
        ActionSettingsError,
        ModelArtifactError,
        ModelCatalogError,
        ModelRepositoryError,
        ModelSettingsError,
        ModelSnapshotError,
    ) as error:
        if gateway is not None:
            gateway.stop()
        _restore_setup_file(project, snapshot)
        if source == "local":
            print("Setup did not prepare a usable local model.")
            print(f"Details: {error}")
            print(
                "Run `heartwood setup` to choose a recommended model or Other Hugging Face "
                "model, or start an existing OpenAI-compatible service."
            )
            print("Then run `heartwood launch`.")
        else:
            print(f"Setup could not validate the model route: {error}")
        return 1, None
    except BaseException:
        if gateway is not None:
            gateway.stop()
        _restore_setup_file(project, snapshot)
        raise
    print("Setup complete.")
    return 0, gateway


def _configure_local_model(
    gateway: SessionGateway,
    *,
    model_id: str | None,
    non_interactive: bool,
) -> None:
    local_catalog = gateway.model_artifacts()
    raw_recommendations = local_catalog.get("models", [])
    recommendations = (
        [
            item
            for item in raw_recommendations
            if isinstance(item, dict) and item.get("available") is True
        ]
        if isinstance(raw_recommendations, list)
        else []
    )
    service_models: list[str] = []
    try:
        service_catalog = gateway.discover_models("local", refresh=True)
    except ModelCatalogError:
        pass
    else:
        raw_service_models = service_catalog.get("models", [])
        if isinstance(raw_service_models, list):
            service_models = [
                str(item["model_id"])
                for item in raw_service_models
                if isinstance(item, dict)
                and isinstance(item.get("model_id"), str)
                and item.get("availability") != "unsupported"
            ]

    if model_id is None:
        print("\nLocal models:")
        choices: list[tuple[str, str]] = []
        for item in recommendations:
            recommendation_id = str(item.get("model_id"))
            label = str(item.get("label"))
            runtime = "CPU" if item.get("runtime") == "llama-cpp" else "NVIDIA GPU"
            source = (
                "Recommended"
                if item.get("catalog_source") == "recommended"
                else "Previously selected"
            )
            choices.append((recommendation_id, f"{label} ({source}, {runtime})"))
        choices.append(("other", "Other Hugging Face model"))
        choices.extend((model, f"{model} (already running)") for model in service_models)
        for index, (_value, label) in enumerate(choices, start=1):
            print(f"  {index}. {label}")
        try:
            selected = input("Select a model by number or enter owner/model: ").strip()
        except EOFError as error:
            raise ModelRepositoryError(
                "local model selection was cancelled because input closed"
            ) from error
        if selected.isdigit() and 1 <= int(selected) <= len(choices):
            model_id = choices[int(selected) - 1][0]
        else:
            model_id = selected
        if model_id == "other":
            try:
                model_id = input("Hugging Face model (owner/model): ").strip()
            except EOFError as error:
                raise ModelRepositoryError(
                    "local model selection was cancelled because input closed"
                ) from error
    if not model_id.strip():
        raise ModelRepositoryError("a local model must be selected")

    known_local_ids = {
        str(item.get("model_id")): item for item in recommendations if item.get("model_id")
    }
    if model_id in known_local_ids:
        item = known_local_ids[model_id]
        print("\nSelected local model")
        print(f"  {item.get('label')}")
        if resources := item.get("recommended_resource_envelope"):
            print(f"  {resources}")
        _run_with_progress(
            lambda: gateway.download_local_model_now(model_id),
            activity=_MODEL_DOWNLOAD_ACTIVITY,
        )
        return
    if model_id in service_models:
        gateway.connect_model("local", model_id)
        return
    if "/" in model_id:
        plan = gateway.inspect_model_repository(model_id)
        print()
        print(_format_model_repository(plan))
        print()
        _run_with_progress(
            lambda: gateway.download_custom_local_model_now(model_id),
            activity=_MODEL_DOWNLOAD_ACTIVITY,
        )
        return
    qualifier = " in non-interactive setup" if non_interactive else ""
    raise ModelRepositoryError(
        f"unknown local model{qualifier}: {model_id}; choose a recommended id, "
        "an owner/model identifier, or a model reported by the local service"
    )


def _prompt_for_provider_token(
    gateway: SessionGateway,
    *,
    connection_id: str,
    non_interactive: bool,
) -> str | None:
    raw_connections = gateway.model_settings().get("connections", [])
    connections = raw_connections if isinstance(raw_connections, list) else []
    connection = next(
        (
            item
            for item in connections
            if isinstance(item, dict) and item.get("connection_id") == connection_id
        ),
        None,
    )
    if connection is None:
        raise ModelCatalogError(f"unknown model connection: {connection_id}")
    if connection.get("credential_status") != "missing" or not connection.get("accepts_token"):
        return None
    if non_interactive:
        return None
    try:
        token = getpass.getpass(f"{connection.get('label', 'Provider')} token: ")
    except EOFError as error:
        raise ModelCatalogError("credential entry was cancelled because input closed") from error
    if not token.strip():
        raise ModelCatalogError("provider token must not be empty")
    return token


def _snapshot_setup_file(project: ProjectContext) -> bytes | None:
    return project.config_path.read_bytes() if project.config_path.is_file() else None


def _restore_setup_file(project: ProjectContext, previous: bytes | None) -> None:
    if previous is None:
        project.config_path.unlink(missing_ok=True)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".config.toml.", dir=project.state_root)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(previous)
        temporary_path.chmod(0o600)
        temporary_path.replace(project.config_path)
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
        if command == "local":
            print(_format_model_artifacts(gateway.model_artifacts()))
            return 0
        if command == "inspect":
            print(
                _format_model_repository(
                    gateway.inspect_model_repository(
                        args.repository,
                        revision=args.revision,
                    )
                )
            )
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
            if "/" not in args.model:
                if args.revision is not None:
                    parser.error("--revision requires a Hugging Face owner/model identifier")
                path = _run_with_progress(
                    lambda: gateway.download_local_model_now(args.model),
                    activity=_MODEL_DOWNLOAD_ACTIVITY,
                )
            else:
                path = _run_with_progress(
                    lambda: gateway.download_custom_local_model_now(
                        args.model,
                        revision=args.revision,
                    ),
                    activity=_MODEL_DOWNLOAD_ACTIVITY,
                )
            print(f"Local model: {path}")
            print("Run `heartwood launch` to start the model and open Heartwood.")
            return 0
    except (
        ModelArtifactError,
        ModelCatalogError,
        ModelRepositoryError,
        ModelSettingsError,
        ModelSnapshotError,
    ) as error:
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
    lines = ["Heartwood local models", ""]
    models = catalog.get("models", [])
    if isinstance(models, list):
        for item in models:
            if not isinstance(item, dict):
                continue
            size = item.get("size_bytes")
            size_gib = float(size) / (1024**3) if isinstance(size, int | float) else 0
            runtime = "CPU" if item.get("runtime") == "llama-cpp" else "NVIDIA GPU"
            review = (
                "Recommended" if item.get("catalog_source") == "recommended" else "User selected"
            )
            lines.append(f"{item.get('model_id')}  {runtime}  {size_gib:.2f} GiB  {review}")
            lines.append(f"    {item.get('label')}: {item.get('purpose')}")
            context_window = item.get("context_window")
            if isinstance(context_window, int):
                lines.append(f"    Context: {context_window:,} tokens")
            lines.append(f"    {item.get('availability_reason')}")
            resources = item.get("recommended_resource_envelope")
            if isinstance(resources, str):
                lines.append(f"    {resources}")
    lines.extend(
        (
            "",
            "Other Hugging Face model:",
            "  heartwood models inspect <owner/model>",
            "  heartwood models download <owner/model>",
        )
    )
    return "\n".join(lines)


def _format_model_repository(inspection: dict[str, object]) -> str:
    model = inspection.get("model", {})
    if not isinstance(model, dict):
        return "Hugging Face model\n\nHeartwood returned an invalid model plan."
    size = model.get("size_bytes")
    size_gib = float(size) / (1024**3) if isinstance(size, int | float) else 0
    context_window = model.get("context_window")
    context_label = f"{context_window:,} tokens" if isinstance(context_window, int) else "Unknown"
    runtime = "CPU" if model.get("runtime") == "llama-cpp" else "NVIDIA GPU"
    lines = [
        "Heartwood model plan",
        "",
        f"Model: {model.get('label')}",
        f"Repository: {model.get('source_repository')}",
        f"Revision: {model.get('source_revision')}",
        f"Runtime: {runtime}",
        f"Download: {size_gib:.2f} GiB",
        f"Context: {context_label}",
        f"Selection: {inspection.get('selection_reason')}",
        f"License: {model.get('license_posture')}",
        "",
        str(model.get("minimum_resource_envelope") or "Resource estimate unavailable."),
        str(model.get("recommended_resource_envelope") or ""),
    ]
    lines.extend(
        (
            "",
            "These models are user selected. Heartwood verifies source integrity but does not "
            "review capability, license, or suitability.",
        )
    )
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
    return _run_with_progress(
        lambda: session.submit(line),
        activity=interaction_activity(line),
        update_interval=update_interval,
    )


def _run_with_progress[Result](
    operation: Callable[[], Result],
    *,
    activity: InteractionActivity,
    update_interval: float = 15,
) -> Result:
    """Run one blocking operation with animated TTY or line-safe status updates."""
    stopped = threading.Event()
    started = time.monotonic()
    animated = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    frames = (".  ", ".. ", "...")
    frame = 0

    def report_progress() -> None:
        nonlocal frame
        interval = min(update_interval, 0.4) if animated else update_interval
        while not stopped.wait(max(interval, 0.01)):
            elapsed = int(time.monotonic() - started)
            if animated:
                label = activity.label if elapsed < 10 else activity.waiting_label
                suffix = "" if elapsed < 10 else f" ({elapsed}s elapsed)"
                marker = frames[frame % len(frames)]
                frame += 1
                print(f"\r\033[2K{label}{marker}{suffix}", end="", flush=True)
            else:
                print(
                    f"{activity.waiting_label} ({elapsed}s elapsed). {activity.guidance}",
                    flush=True,
                )

    if animated:
        print(f"{activity.label}{frames[0]}", end="", flush=True)
    else:
        print(f"{activity.label}...", flush=True)
    reporter = threading.Thread(
        target=report_progress,
        name="heartwood-line-progress",
        daemon=True,
    )
    reporter.start()
    try:
        return operation()
    finally:
        stopped.set()
        reporter.join()
        if animated:
            print("\r\033[2K", end="", flush=True)


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
            if argument_lines := format_action_arguments(action.arguments):
                lines.append("     Arguments:")
                lines.extend(f"       {line}" for line in argument_lines)
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
        line = (
            f"{prefix} Action: {event.payload.get('summary', event.payload.get('tool_name', ''))} "
            f"(risk={event.payload.get('risk', 'unknown')})"
        )
        arguments = event.payload.get("arguments")
        if not isinstance(arguments, dict):
            return line
        argument_lines = format_action_arguments(arguments)
        if not argument_lines:
            return line
        return "\n".join((line, "  Arguments:", *(f"    {item}" for item in argument_lines)))
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


def _handle_detect(
    gateway: SessionGateway,
    *,
    project: ProjectContext,
    session_id: str,
) -> int:
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
    print(f"Project: {project.root}")
    print(f"State: {project.state_root}")
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
    project: ProjectContext,
    session_id: str,
    fixture_root: Path,
    output: Path,
) -> int:
    packet = ReviewerPacketGenerator(
        repository_root=_bundled_repository_root(),
        session_workspace=project.sessions_dir,
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
    project: ProjectContext,
    host: str,
    port: int,
    web_root: Path,
    base_path: str,
) -> int:
    if not web_root.exists():
        msg = f"web UI assets not found: {web_root}"
        raise SystemExit(msg)
    app = GatewayAsgiApp(
        SessionGateway(project=project),
        static_dir=web_root,
        static_base_path=base_path,
    )
    if select_platform_adapter(os.environ).adapter_id == "terra":
        print("Heartwood web interface")
        if has_authenticated_jupyter_proxy():
            print(f"Terra browser path: {jupyter_proxy_url(port=port)}")
        else:
            print("Terra browser path unavailable in this terminal.")
            print("Open the tutorial notebook to generate the authenticated browser link.")
        print("Keep this terminal open while using the browser.")
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
