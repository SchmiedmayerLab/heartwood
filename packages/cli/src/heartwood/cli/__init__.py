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
    format_action_settings,
    format_projection_lines,
    interaction_activity,
)
from heartwood.cli._launch import LaunchOptions, run_launch
from heartwood.gateway import (
    ACTION_MODE_OPTIONS,
    BUILT_IN_MODEL_CONNECTIONS,
    DEFAULT_SESSION_ID,
    MODEL_SOURCE_OPTIONS,
    ActionSettingsError,
    CommandConflictError,
    CredentialStoreError,
    DeploymentReadiness,
    GatewayAsgiApp,
    InterfaceKind,
    ModelArtifactError,
    ModelCatalogError,
    ModelProfile,
    ModelRepositoryError,
    ModelSettingsError,
    ModelSnapshotError,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    ProjectStateError,
    SessionGateway,
    SessionOwnershipError,
    SessionRecoveryError,
    SkillSettingsError,
    StartupPlan,
    action_mode_label,
    custom_model_connection_requires_token,
    inspect_deployment,
    model_source_options,
)
from heartwood.schemas import (
    ModelArtifactsResponse,
    ModelCatalogResponse,
    ModelRepositoryPlanResponse,
    ModelSettingsResponse,
    ModelValidationResponse,
    SkillSettingsResponse,
    SkillSummaryResponse,
    StartupPlanResponse,
)
from heartwood.session import (
    CommandKind,
    EventKind,
    JsonValue,
    SessionCommand,
    SessionEvent,
    new_command_id,
    validate_session_id,
)

__all__ = ["__version__", "main"]

__version__ = "0.2.0"

_PROG = "heartwood"


def _bundled_path(relative: Path) -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.exists():
            return candidate
    return relative


_DEFAULT_WEB_ROOT = _bundled_path(Path("packages") / "webui" / "dist")
_DEFAULT_FIXTURE_ROOT = _bundled_path(Path("fixtures") / "synthetic")
_ACTION_MODE_ARGUMENTS = {option.command_value: option.mode for option in ACTION_MODE_OPTIONS}
_MODEL_SOURCE_ARGUMENTS = {
    "heartwood": "heartwood",
    "openai-subscription": "openai-subscription",
    "openai": "openai",
    "anthropic": "anthropic",
    "custom": "custom",
    "stanford-ai-api-gateway": "stanford-ai-api-gateway",
}
_MODEL_DOWNLOAD_ACTIVITY = InteractionActivity(
    label="Preparing and verifying the model",
    waiting_label="Still preparing and verifying the model",
    guidance=(
        "Large downloads and full verification of existing model files can take several minutes. "
        "Keep this process running."
    ),
)
_STARTUP_ACTIVITY = InteractionActivity(
    label="Checking the project and environment",
    waiting_label="Still checking the project and environment",
    guidance="Managed environments can take additional time to inspect.",
)
_MODEL_CATALOG_ACTIVITY = InteractionActivity(
    label="Checking available models",
    waiting_label="Still checking available models",
    guidance="Model services and managed environments can take additional time to respond.",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "A coding agent for biomedical research projects, with reviewable actions "
            "and a durable audit history."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{_PROG} {__version__}")
    parser.add_argument(
        "--session-id",
        default=DEFAULT_SESSION_ID,
        type=_session_id_argument,
        help="Session identifier.",
    )
    parser.add_argument(
        "--interface",
        choices=("terminal", "web"),
        default="terminal",
        help="Open the terminal or browser presentation.",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Use the line-oriented terminal instead of the full-screen interface.",
    )
    parser.add_argument("--prompt", "-p", help="Submit one task and exit.")
    parser.add_argument("--prompt-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8767, help="Browser interface port.")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    doctor = subparsers.add_parser("doctor", help="Inspect environment and setup readiness.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable diagnostics.")
    setup = subparsers.add_parser("setup", help="Configure a model route and conservative policy.")
    setup.add_argument(
        "--model-source",
        choices=tuple(option.source_id for option in model_source_options(os.environ)),
        help="Model service to configure.",
    )
    setup.add_argument("--model-id", help="Exact model identifier reported by the service.")
    setup.add_argument("--base-url", help="Base URL for another compatible model service.")
    setup.add_argument(
        "--remember-credential",
        action="store_true",
        help="Store the provider API key in the system credential store when available.",
    )
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="Require explicit inputs and do not prompt.",
    )
    setup.add_argument("--yes", action="store_true", help="Confirm the displayed configuration.")
    setup.add_argument(
        "--yes-download",
        action="store_true",
        help="Confirm the displayed model download without an interactive prompt.",
    )

    runtime = subparsers.add_parser(
        "runtime", help="Advanced Heartwood-managed inference operations."
    )
    runtime_subparsers = runtime.add_subparsers(dest="runtime_command", metavar="<runtime-command>")
    runtime_start = runtime_subparsers.add_parser(
        "start", help="Review and start the selected Heartwood-managed model runtime."
    )
    runtime_start.add_argument(
        "--partition",
        help="Slurm GPU partition; by default Heartwood selects the available default.",
    )
    runtime_start.add_argument(
        "--gpus",
        type=int,
        help="Advanced: override the catalog model's qualified GPU count.",
    )
    runtime_start.add_argument("--cpus", type=int)
    runtime_start.add_argument("--memory")
    runtime_start.add_argument("--time", dest="time_limit", default="02:00:00")
    runtime_start.add_argument(
        "--task-profile",
        choices=("auto", "standard", "powerful", "maximum"),
        default="auto",
        help="Capability tier used when Heartwood recommends a model.",
    )
    runtime_start.add_argument("--startup-timeout", type=int, default=600)
    runtime_start.add_argument("--dry-run", action="store_true")
    runtime_start.add_argument("--no-allocate", action="store_true")
    runtime_start.add_argument(
        "--yes-request-allocation",
        action="store_true",
        help="Confirm the displayed scheduler request without an interactive prompt.",
    )
    runtime_start.add_argument(
        "--yes-download",
        action="store_true",
        help="Confirm the displayed pinned model download without an interactive prompt.",
    )
    runtime_start.add_argument("--inside-allocation", action="store_true", help=argparse.SUPPRESS)

    allow = subparsers.add_parser(
        "allow",
        aliases=["approve"],
        help="Allow the complete pending OpenHands action set once.",
    )
    allow.add_argument(
        "action_group_id",
        nargs="?",
        help="Optional action-set identifier for automation.",
    )
    reject = subparsers.add_parser(
        "reject",
        aliases=["deny"],
        help="Reject the complete pending OpenHands action set.",
    )
    reject.add_argument(
        "action_group_id",
        nargs="?",
        help="Optional action-set identifier for automation.",
    )
    subparsers.add_parser("pause", help="Pause the current session.")
    subparsers.add_parser("resume", help="Resume the current session.")
    subparsers.add_parser("replay", help="Replay the persisted session event stream.")

    actions = subparsers.add_parser("actions", help="Configure action review.")
    action_subparsers = actions.add_subparsers(dest="actions_command", metavar="<actions-command>")
    action_set = action_subparsers.add_parser(
        "set", help="Select an action-review mode allowed by platform policy."
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
    model_subparsers.add_parser(
        "managed", help="List models that Heartwood can manage in this environment."
    )
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
    add.add_argument("--base-url", help="Custom provider or loopback OpenAI-compatible base URL.")
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
    forget = model_subparsers.add_parser(
        "forget", help="Remove a saved provider credential from the system credential store."
    )
    forget.add_argument("connection_id")
    download = model_subparsers.add_parser(
        "download", help="Download a recommended or inspected Hugging Face model."
    )
    download.add_argument("model", help="Default model id or Hugging Face owner/model identifier.")
    download.add_argument(
        "--revision",
        help="Advanced: repository branch, tag, or commit for an owner/model identifier.",
    )
    import_model = model_subparsers.add_parser(
        "import", help="Import an existing GGUF file or vLLM model directory."
    )
    import_model.add_argument("path", type=Path, help="Existing model file or directory.")
    import_model.add_argument(
        "--source",
        required=True,
        help="Upstream Hugging Face owner/model identifier.",
    )
    import_model.add_argument(
        "--revision",
        required=True,
        help="Immutable upstream commit hash.",
    )
    import_model.add_argument(
        "--license",
        required=True,
        dest="license_posture",
        help="Upstream license identifier or review note.",
    )
    import_model.add_argument(
        "--context-window",
        type=int,
        default=32_768,
        help="Maximum model context supported by this artifact.",
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

    gateway = subparsers.add_parser("gateway", help="Advanced gateway operations.")
    gateway_subparsers = gateway.add_subparsers(dest="gateway_command", metavar="<gateway-command>")
    gateway_serve = gateway_subparsers.add_parser(
        "serve", help="Serve the gateway and packaged browser interface."
    )
    gateway_serve.add_argument("--host", default="127.0.0.1", help="Gateway bind host.")
    gateway_serve.add_argument("--port", type=int, default=8767, help="Gateway bind port.")
    gateway_serve.add_argument("--web-root", type=Path, default=_DEFAULT_WEB_ROOT)
    gateway_serve.add_argument("--base-path", default="/", help="Base path behind a proxy.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``heartwood`` and return a process exit code."""
    try:
        return _main(argv)
    except (CommandConflictError, SessionOwnershipError, SessionRecoveryError) as error:
        print(f"Session unavailable: {error}", file=sys.stderr)
        return 75
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


def _main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    project = ProjectContext.current()
    if args.port < 1 or args.port > 65_535:
        parser.error("--port must be between 1 and 65535")
    if args.plain and args.interface != "terminal":
        parser.error("--plain can be used only with --interface terminal")
    if args.prompt is not None and args.prompt_file is not None:
        parser.error("--prompt and the internal prompt handoff cannot be combined")
    has_prompt = args.prompt is not None or args.prompt_file is not None
    if has_prompt and args.interface != "terminal":
        parser.error("--prompt can be used only with --interface terminal")
    if (
        args.command is None
        and args.interface == "terminal"
        and not sys.stdin.isatty()
        and not has_prompt
    ):
        parser.print_help()
        return 0
    if args.command == "gateway" and args.gateway_command == "serve":
        return _handle_serve(
            project=project,
            host=args.host,
            port=args.port,
            web_root=args.web_root,
            base_path=args.base_path,
        )
    if args.command == "doctor":
        return _handle_doctor(project=project, as_json=args.json)
    if args.command == "setup":
        return _handle_setup(parser, args, project=project)
    if args.command == "runtime" and args.runtime_command == "start":
        if args.gpus is not None and args.gpus < 1:
            parser.error("--gpus must be positive")
        if args.cpus is not None and args.cpus < 1:
            parser.error("--cpus must be positive")
        if args.startup_timeout < 1 or args.port < 1:
            parser.error("--startup-timeout and --port must be positive")
        return run_launch(_launch_options(project, args))
    configured_gateway: SessionGateway | None = None
    if args.command is None:
        startup_gateway, startup = _run_with_progress(
            lambda: _inspect_startup(project, interface=args.interface, port=args.port),
            activity=_STARTUP_ACTIVITY,
        )
        if not startup.interface_supported or startup.phase == "recovery-required":
            print(_format_startup_plan(startup))
            startup_gateway.stop()
            return 1
        if args.interface == "web" and startup.phase != "ready" and not startup.requires_compute:
            print(_format_startup_plan(startup))
            print("\nOpening guided setup in the browser. Keep this terminal running.")
            startup_gateway.stop()
            return _handle_serve(
                project=project,
                host=args.host,
                port=args.port,
                web_root=_DEFAULT_WEB_ROOT,
                base_path="/",
            )
        if startup.phase == "project-review" and not _review_project(project):
            print("No project files were changed.")
            startup_gateway.stop()
            return 0
        if startup.phase in {
            "project-review",
            "connection-required",
            "credential-required",
            "model-required",
        }:
            print(f"{startup.summary}\n")
            startup_gateway.stop()
            setup_code, configured_gateway = _configure_setup(parser, args, project=project)
            if setup_code != 0:
                return setup_code
            print("\nSetup complete. Starting Heartwood.")
            if configured_gateway is None:  # pragma: no cover - setup success invariant
                raise RuntimeError("setup completed without a gateway")
            startup = configured_gateway.startup(interface=args.interface, port=args.port)
        else:
            configured_gateway = startup_gateway
        if startup.phase == "recovery-required":
            print(_format_startup_plan(startup))
            if configured_gateway is not None:
                configured_gateway.stop()
            return 1
        if startup.phase == "compute-required":
            if configured_gateway is not None:
                configured_gateway.stop()
            print(_format_startup_plan(startup))
            print()
            return run_launch(_launch_options(project, args))
        if args.interface == "web":
            if configured_gateway is not None:
                configured_gateway.stop()
            print(_format_startup_plan(startup))
            return _handle_serve(
                project=project,
                host=args.host,
                port=args.port,
                web_root=_DEFAULT_WEB_ROOT,
                base_path="/",
            )

    gateway = configured_gateway or _run_with_progress(
        lambda: SessionGateway(project=project),
        activity=_STARTUP_ACTIVITY,
    )
    gateway.start()
    try:
        if args.command == "models":
            return _handle_models(parser, gateway, args)
        if args.command == "actions":
            return _handle_actions(parser, gateway, args)
        if args.command == "skills":
            return _handle_skills(parser, gateway, args)
        if args.command is None:
            if has_prompt:
                try:
                    prompt = _consume_prompt(project, args.prompt, args.prompt_file)
                except ProjectStateError as error:
                    print(f"Pending task unavailable: {error}")
                    return 64
                return _submit_task(gateway, session_id=args.session_id, prompt=prompt)
            return _interactive_chat(
                gateway,
                session_id=args.session_id,
                plain=args.plain,
            )
        if args.command in {"allow", "approve", "reject", "deny"}:
            directive = "/allow" if args.command in {"allow", "approve"} else "/reject"
            if args.action_group_id:
                directive = f"{directive} {shlex.quote(args.action_group_id)}"
            result = _run_with_progress(
                lambda: _submit_and_wait(
                    InteractiveSession(gateway, session_id=args.session_id),
                    directive,
                ),
                activity=interaction_activity(directive),
            )
            if result.message:
                print(result.message)
            if result.projection is not None:
                print("\n".join(format_projection_lines(result.projection)))
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
    gateway = SessionGateway(project=project)
    readiness = gateway.deployment_readiness()
    gateway.stop()
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
        if check.status != "pass":
            diagnostic = check.safe_dict()
            lines.append(f"       {diagnostic['code']} · {diagnostic['next_action']}")
    return "\n".join(lines)


def _format_startup_plan(startup: StartupPlan) -> str:
    plan = startup.safe_dict()
    capabilities = plan["capabilities"]
    if not isinstance(capabilities, dict):  # pragma: no cover - typed serialization invariant
        raise TypeError("startup capabilities must be an object")
    return "\n".join(
        (
            "Heartwood",
            f"Project: {plan['project_root']}",
            f"Environment: {capabilities['display_name']}",
            f"Interface: {plan['interface']}",
            "",
            str(plan["summary"]),
            f"Next: {plan['next_action']}",
        )
    )


def _inspect_startup(
    project: ProjectContext,
    *,
    interface: InterfaceKind,
    port: int,
) -> tuple[SessionGateway, StartupPlan]:
    gateway = SessionGateway(project=project)
    try:
        return gateway, gateway.startup(interface=interface, port=port)
    except BaseException:
        gateway.stop()
        raise


def _launch_options(project: ProjectContext, args: argparse.Namespace) -> LaunchOptions:
    return LaunchOptions(
        project=project,
        session_id=args.session_id,
        partition=getattr(args, "partition", None),
        gpus=getattr(args, "gpus", None),
        cpus=getattr(args, "cpus", None),
        memory=getattr(args, "memory", None),
        time_limit=getattr(args, "time_limit", "02:00:00"),
        task_profile=getattr(args, "task_profile", "auto"),
        dry_run=getattr(args, "dry_run", False),
        no_allocate=getattr(args, "no_allocate", False),
        yes_request_allocation=getattr(args, "yes_request_allocation", False),
        yes_download=getattr(args, "yes_download", False),
        inside_allocation=getattr(args, "inside_allocation", False),
        plain=args.plain,
        web=args.interface == "web",
        web_host=args.host,
        web_port=args.port,
        startup_timeout=getattr(args, "startup_timeout", 600),
        prompt=args.prompt,
        prompt_file=args.prompt_file,
    )


def _consume_prompt(project: ProjectContext, prompt: str | None, prompt_file: Path | None) -> str:
    if prompt is not None:
        return prompt
    if prompt_file is None:  # pragma: no cover - guarded by the caller
        raise ProjectStateError("no pending task was provided")
    if prompt_file.is_symlink():
        raise ProjectStateError("the pending task must not be a symbolic link")
    try:
        resolved = prompt_file.resolve(strict=True)
        runtime_dir = project.runtime_dir.resolve(strict=True)
    except OSError as error:
        raise ProjectStateError("the pending task file is unavailable") from error
    if resolved.parent != runtime_dir or not resolved.name.startswith("pending-prompt."):
        raise ProjectStateError("the pending task is outside this project's private runtime state")
    if not resolved.is_file():
        raise ProjectStateError("the pending task is not a regular file")
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ProjectStateError("the pending task could not be read") from error
    finally:
        resolved.unlink(missing_ok=True)


def _review_project(project: ProjectContext) -> bool:
    print("Heartwood project")
    print(f"  {project.root}")
    print("Heartwood can work with files in this folder and its subfolders.")
    entries = tuple(path for path in project.root.iterdir() if path.name != ".heartwood")
    print("\nChoose how to begin:")
    print("  1. Use this project")
    if not entries:
        print("  2. Add the synthetic first example")
    print("  0. Cancel")
    try:
        choice = input("Select: ").strip()
    except EOFError:
        return False
    if choice in {"", "1"}:
        return True
    if choice == "2" and not entries:
        _create_synthetic_example(project)
        return True
    return False


def _create_synthetic_example(project: ProjectContext) -> None:
    source = _DEFAULT_FIXTURE_ROOT / "omop-like"
    destination = project.root / "data"
    if destination.exists():
        raise ProjectStateError("synthetic example destination already exists: data")
    destination.mkdir(mode=0o700)
    for filename in ("person.csv", "condition_occurrence.csv"):
        shutil.copy2(source / filename, destination / filename)
    print("Added synthetic data under data/. No real research data was accessed.")


def _handle_setup(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    project: ProjectContext,
) -> int:
    code, gateway = _configure_setup(parser, args, project=project)
    startup: StartupPlanResponse | None = None
    process_only_credentials = False
    if gateway is not None:
        startup = gateway.startup_plan(interface="terminal")
        credential_settings = gateway.credential_settings()
        process_only_credentials = any(
            binding["configured"] and binding["source"] == "process"
            for binding in credential_settings["bindings"]
        )
        gateway.stop()
    if code == 0:
        if process_only_credentials:
            print("Configuration saved, but the provider API key was not stored.")
            print(
                "Export the provider credential in this shell or rerun setup with "
                "--remember-credential before starting a new Heartwood process."
            )
            return 2
        print("Setup complete.")
        if startup is not None and startup["phase"] == "compute-required":
            print("Run `heartwood` to start the selected model and conversation.")
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
    source_argument = getattr(args, "model_source", None)
    source = _MODEL_SOURCE_ARGUMENTS[source_argument] if source_argument is not None else None
    base_url = getattr(args, "base_url", None)
    non_interactive = bool(getattr(args, "non_interactive", False))
    confirmed = bool(getattr(args, "yes", False))
    model_id = getattr(args, "model_id", None)
    remember_credential = bool(getattr(args, "remember_credential", False))
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
                if source == "heartwood" and existing.local_model is not None:
                    model_id = existing.local_model.artifact_id
                    resume_managed_local = True
                else:
                    if source == "custom" and base_url is None:
                        base_url = existing_profile.base_url
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
        options = model_source_options(os.environ)
        print("\nWhere should the model run?")
        for index, option in enumerate(options, start=1):
            print(f"  {index}. {option.label}")
            print(f"     {option.description}")
        try:
            choice = input(f"Select [1-{len(options)}]: ").strip()
        except EOFError:
            print("\nSetup cancelled because input closed.")
            return 1, None
        if not choice.isdigit() or not 1 <= int(choice) <= len(options):
            print("Setup cancelled because no valid model source was selected.")
            return 1, None
        source = options[int(choice) - 1].source_id
    if non_interactive and model_id is None:
        parser.error("--model-id is required with --non-interactive")
    print("\nConfiguration")
    print(f"  Platform: {readiness.platform_id}")
    source_option = next(
        (option for option in MODEL_SOURCE_OPTIONS if option.source_id == source),
        None,
    )
    print(f"  Model source: {source_option.label if source_option else source}")
    print(
        "  Action review: Existing project setting"
        if resume_existing
        else f"  Action review: {action_mode_label('always-confirm')}"
    )
    if not confirmed and not resume_existing:
        if non_interactive:
            parser.error("--yes is required with --non-interactive")
        try:
            confirmed = input("Continue with this model setup? [y/N]: ").strip().lower() == "y"
        except EOFError:
            print("\nSetup cancelled because input closed.")
            return 1, None
    if not confirmed and not resume_existing:
        print("Setup cancelled.")
        return 1, None
    snapshot = _snapshot_setup_file(project)
    gateway: SessionGateway | None = None
    try:
        gateway = _run_with_progress(
            lambda: SessionGateway(project=project),
            activity=_STARTUP_ACTIVITY,
        )
        if not resume_existing:
            gateway.configure_model_source(source)
        gateway.start()
        if not resume_existing:
            gateway.select_action_confirmation_mode("always-confirm")
        if source == "heartwood":
            if not resume_managed_local:
                _configure_local_model(
                    gateway,
                    model_id=model_id,
                    non_interactive=non_interactive,
                    yes_download=bool(getattr(args, "yes_download", False)),
                )
            return 0, gateway
        source_option = next(
            option for option in MODEL_SOURCE_OPTIONS if option.source_id == source
        )
        connection_id = source_option.connection_id
        if source == "custom" and base_url is None and not non_interactive:
            try:
                base_url = input("Compatible service URL: ").strip()
            except EOFError as error:
                raise ModelCatalogError("service URL entry was cancelled") from error
        if source == "custom" and not base_url:
            raise ModelCatalogError("other compatible services require --base-url")
        requires_token = source != "openai-subscription"
        if source == "custom":
            assert isinstance(base_url, str)
            requires_token = custom_model_connection_requires_token(base_url)
        token = (
            _prompt_for_provider_token(
                gateway,
                connection_id=connection_id,
                non_interactive=non_interactive,
            )
            if requires_token
            else None
        )
        if token is not None and not non_interactive:
            credential_store = gateway.credential_settings()["store"]
            if credential_store["persistence_available"] and source != "custom":
                try:
                    remember_credential = (
                        input("Remember this API key in the system credential store? [y/N]: ")
                        .strip()
                        .lower()
                        == "y"
                    )
                except EOFError as error:
                    raise ModelCatalogError("credential storage choice was cancelled") from error
            elif source != "custom":
                print("The API key will be kept only until this Heartwood command exits.")
        catalog = _run_with_progress(
            lambda: gateway.discover_models(
                connection_id,
                token=token,
                base_url=base_url,
                refresh=True,
                remember=remember_credential,
            ),
            activity=_MODEL_CATALOG_ACTIVITY,
        )
        available = [
            item["model_id"] for item in catalog["models"] if item["availability"] != "unsupported"
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
        if source == "openai-subscription":
            credential_status = catalog["connection"]["credential_status"]
            if credential_status != "available":
                print("\nSign in with ChatGPT")
                print("OpenHands will show OpenAI's terms, then provide a URL and one-time code.")
                gateway.login_subscription(
                    connection_id,
                    model_id=model_id,
                    open_browser=False,
                    auth_method="device_code",
                )
        gateway.connect_model(connection_id, model_id, base_url=base_url)
    except (
        ActionSettingsError,
        ModelArtifactError,
        ModelCatalogError,
        CredentialStoreError,
        ModelRepositoryError,
        ModelSettingsError,
        ModelSnapshotError,
    ) as error:
        if gateway is not None:
            gateway.stop()
        _restore_setup_file(project, snapshot)
        if source == "heartwood":
            print("Setup did not prepare a usable Heartwood-managed model.")
            print(f"Details: {error}")
            print(
                "Run `heartwood setup` to choose a recommended model or Other Hugging Face "
                "model, or start an existing OpenAI-compatible service."
            )
            print("Then run `heartwood`.")
        else:
            print(f"Setup could not validate the model route: {error}")
        return 1, None
    except BaseException:
        if gateway is not None:
            gateway.stop()
        _restore_setup_file(project, snapshot)
        raise
    return 0, gateway


def _configure_local_model(
    gateway: SessionGateway,
    *,
    model_id: str | None,
    non_interactive: bool,
    yes_download: bool,
) -> None:
    local_catalog, service_models = _run_with_progress(
        lambda: _available_managed_models(gateway),
        activity=_MODEL_CATALOG_ACTIVITY,
    )
    recommendations = [item for item in local_catalog["models"] if item["available"]]
    if model_id is None:
        print("\nModels Heartwood can run:")
        choices: list[tuple[str, str]] = []
        for item in recommendations:
            recommendation_id = item["model_id"]
            label = item["label"]
            runtime = "CPU" if item["runtime"] == "llama-cpp" else "NVIDIA GPU"
            if item["recommended"]:
                source = "Heartwood recommendation"
            elif item["catalog_source"] == "catalog":
                source = "Under evaluation"
            else:
                source = "Previously selected"
            tier = _model_tier_label(item["tier"])
            choices.append((recommendation_id, f"{tier}: {label} ({source}, {runtime})"))
        choices.append(("other", "Other Hugging Face model"))
        choices.extend((model, f"{model} (already running)") for model in service_models)
        for index, (_value, label) in enumerate(choices, start=1):
            print(f"  {index}. {label}")
        try:
            selected = input("Select a model by number or enter owner/model: ").strip()
        except EOFError as error:
            raise ModelRepositoryError(
                "Heartwood-managed model selection was cancelled because input closed"
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
                    "Heartwood-managed model selection was cancelled because input closed"
                ) from error
    if not model_id.strip():
        raise ModelRepositoryError("a Heartwood-managed model must be selected")

    known_local_ids = {item["model_id"]: item for item in recommendations}
    if model_id in known_local_ids:
        item = known_local_ids[model_id]
        print("\nSelected Heartwood-managed model")
        print(f"  {item['label']}")
        print(f"  Hugging Face: {item['source_repository']}")
        print(f"  Pinned revision: {item['source_revision']}")
        print(f"  Download: {item['size_bytes'] / 1024**3:.2f} GiB")
        if resources := item["recommended_resource_envelope"]:
            print(f"  {resources}")
        _confirm_model_download(
            label=item["label"],
            non_interactive=non_interactive,
            yes_download=yes_download,
        )
        _run_with_progress(
            lambda: gateway.download_local_model_now(model_id),
            activity=_MODEL_DOWNLOAD_ACTIVITY,
        )
        return
    if model_id in service_models:
        gateway.connect_model("heartwood", model_id)
        return
    if "/" in model_id:
        plan = gateway.inspect_model_repository(model_id)
        print()
        print(_format_model_repository(plan))
        print()
        _confirm_model_download(
            label=plan["model"]["label"],
            non_interactive=non_interactive,
            yes_download=yes_download,
        )
        _run_with_progress(
            lambda: gateway.download_custom_local_model_now(model_id),
            activity=_MODEL_DOWNLOAD_ACTIVITY,
        )
        return
    qualifier = " in non-interactive setup" if non_interactive else ""
    raise ModelRepositoryError(
        f"unknown Heartwood-managed model{qualifier}: {model_id}; choose a recommended id, "
        "an owner/model identifier, or a model reported by the Heartwood runtime"
    )


def _confirm_model_download(
    *,
    label: str,
    non_interactive: bool,
    yes_download: bool,
) -> None:
    if yes_download:
        return
    if non_interactive:
        raise ModelRepositoryError(
            "model weights are downloaded only after explicit approval; review the model plan "
            "and rerun setup with --yes-download"
        )
    try:
        approved = input(f"Download {label} into .heartwood/models? [y/N]: ").strip().lower()
    except EOFError as error:
        raise ModelRepositoryError("model download approval was cancelled") from error
    if approved != "y":
        raise ModelRepositoryError("model download was not approved")


def _available_managed_models(
    gateway: SessionGateway,
) -> tuple[ModelArtifactsResponse, list[str]]:
    local_catalog = gateway.model_artifacts()
    try:
        service_catalog = gateway.discover_models("heartwood", refresh=True)
    except ModelCatalogError:
        return local_catalog, []
    service_models = [
        item["model_id"]
        for item in service_catalog["models"]
        if item["availability"] != "unsupported"
    ]
    return local_catalog, service_models


def _prompt_for_provider_token(
    gateway: SessionGateway,
    *,
    connection_id: str,
    non_interactive: bool,
) -> str | None:
    connections = gateway.model_settings()["connections"]
    connection = next(
        (item for item in connections if item["connection_id"] == connection_id),
        None,
    )
    if connection is None:
        raise ModelCatalogError(f"unknown model connection: {connection_id}")
    if connection["credential_status"] != "missing" or not connection["accepts_token"]:
        return None
    if non_interactive:
        return None
    try:
        token = getpass.getpass(f"{connection['label']} API key: ")
    except EOFError as error:
        raise ModelCatalogError("credential entry was cancelled because input closed") from error
    if not token.strip():
        raise ModelCatalogError("provider API key must not be empty")
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
        if command == "managed":
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
        if command == "import":
            imported = gateway.import_local_model(
                args.path,
                source_repository=args.source,
                source_revision=args.revision,
                license_posture=args.license_posture,
                context_window=args.context_window,
            )
            print(f"{imported['model']['label']} is ready in this project.")
            print(f"Location: {imported['path']}")
            print("Run `heartwood` to use it.")
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
        if command == "forget":
            gateway.forget_credential(args.connection_id)
            print(f"Forgot the saved credential for {args.connection_id}.")
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
            print(f"Model files are ready: {path}")
            print("Run `heartwood` to continue setup or open Heartwood.")
            return 0
    except (
        ModelArtifactError,
        ModelCatalogError,
        CredentialStoreError,
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
    """Show or update the shared OpenHands action-review mode."""
    try:
        if getattr(args, "actions_command", None) == "set":
            settings = gateway.select_action_confirmation_mode(_ACTION_MODE_ARGUMENTS[args.mode])
        else:
            settings = gateway.action_settings()
    except ActionSettingsError as error:
        parser.error(str(error))
    print(format_action_settings(settings))
    return 0


def _format_model_settings(settings: ModelSettingsResponse) -> str:
    lines = ["Heartwood models", "", "Connections:"]
    for connection in settings["connections"]:
        lines.append(
            f"  {connection['connection_id']}  {connection['label']}  "
            f"source={connection['source']}  credentials={connection['credential_status']}"
        )
    lines.extend(("", "Active and saved profiles:"))
    active = settings["active_profile"]
    profiles = settings["profiles"]
    if profiles:
        for profile in profiles:
            profile_id = profile["profile_id"]
            marker = "*" if profile_id == active else " "
            lines.append(
                f"{marker} {profile_id}  {profile['model']}  "
                f"credentials={profile.get('credential_status', 'unknown')}"
            )
            lines.append(f"    policy endpoint: {profile['policy_endpoint']}")
    else:
        lines.append("No model profiles configured.")
    return "\n".join(lines)


def _format_model_catalog(catalog: ModelCatalogResponse) -> str:
    connection = catalog["connection"]
    lines = [f"Models available from {connection['label']}", ""]
    models = catalog["models"]
    if not models:
        return "\n".join((*lines, "No models available."))
    for item in models:
        model_id = item["model_id"]
        display_name = item["display_name"]
        label = model_id if display_name in {None, model_id} else f"{display_name} ({model_id})"
        lines.append(f"  {label}  [{item['availability']}]")
        lines.append(f"    {item['reason']}")
    lines.extend(
        (
            "",
            f"Select with: heartwood models connect {connection['connection_id']} <model-id>",
        )
    )
    return "\n".join(lines)


def _format_model_validation(validation: ModelValidationResponse) -> str:
    profile = validation["profile"]
    decision = validation["policy_decision"]
    return "\n".join(
        (
            f"Profile: {profile['profile_id']}",
            f"Model: {profile['model']}",
            f"Credentials: {validation['credential_status']}",
            f"Action review: {action_mode_label(validation['action_confirmation_mode'])}",
            f"Policy: {decision['decision']} ({decision['reason']})",
        )
    )


def _format_model_artifacts(catalog: ModelArtifactsResponse) -> str:
    lines = ["Models Heartwood can run", ""]
    for tier in ("standard", "powerful", "maximum"):
        tier_models = [item for item in catalog["models"] if item["tier"] == tier]
        if not tier_models:
            continue
        lines.append(_model_tier_label(tier))
        for item in tier_models:
            size_gib = item["size_bytes"] / (1024**3)
            runtime = "CPU" if item["runtime"] == "llama-cpp" else "NVIDIA GPU"
            if item["recommended"]:
                review = "Recommended"
            elif item["catalog_source"] == "catalog":
                review = "Not tested"
            else:
                review = "User selected"
            lines.append(f"  {item['model_id']}  {runtime}  {size_gib:.2f} GiB  {review}")
            lines.append(f"      {item['label']}: {item['purpose']}")
            lines.append(f"      Context capacity: up to {item['context_window']:,} tokens")
            lines.append(f"      {item['availability_reason']}")
            if resources := item["recommended_resource_envelope"]:
                lines.append(f"      {resources}")
        lines.append("")
    lines.extend(
        (
            "",
            "Other Hugging Face model:",
            "  heartwood models inspect <owner/model>",
            "  heartwood models download <owner/model>",
        )
    )
    return "\n".join(lines)


def _model_tier_label(value: object) -> str:
    if value == "powerful":
        return "Powerful"
    if value == "maximum":
        return "Maximum capability"
    return "Standard"


def _format_model_repository(inspection: ModelRepositoryPlanResponse) -> str:
    model = inspection["model"]
    size_gib = model["size_bytes"] / (1024**3)
    runtime = "CPU" if model["runtime"] == "llama-cpp" else "NVIDIA GPU"
    lines = [
        "Heartwood model plan",
        "",
        f"Model: {model['label']}",
        f"Repository: {model['source_repository']}",
        f"Revision: {model['source_revision']}",
        f"Runtime: {runtime}",
        f"Download: {size_gib:.2f} GiB",
        f"Context capacity: up to {model['context_window']:,} tokens",
        f"Selection: {inspection['selection_reason']}",
        f"License: {model['license_posture']}",
        "",
        model["minimum_resource_envelope"] or "Resource estimate unavailable.",
        model["recommended_resource_envelope"] or "",
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


def _format_skill_settings(settings: SkillSettingsResponse) -> str:
    lines = ["Heartwood Skills", ""]
    skills = settings["skills"]
    if not skills:
        return "\n".join((*lines, "No Skills available."))
    for item in skills:
        lines.append(f"{item['name']}  trust={item['trust_tier']}  source={item['source']}")
        lines.append(f"    {item['description']}")
    return "\n".join(lines)


def _format_skill_summary(summary: SkillSummaryResponse) -> str:
    tool_text = ", ".join(summary["declared_tools"])
    return "\n".join(
        (
            f"Skill: {summary['name']}",
            f"Trust: {summary['trust_tier']}",
            f"Tools: {tool_text}",
            f"Network: {'required' if summary['requires_network'] else 'disabled'}",
            f"Permissions: {summary['approval_summary']}",
        )
    )


def _submit_task(
    gateway: SessionGateway,
    *,
    session_id: str,
    prompt: str,
) -> int:
    session = InteractiveSession(gateway, session_id=session_id)
    result = _run_with_progress(
        lambda: _submit_and_wait(session, prompt),
        activity=interaction_activity(prompt),
    )
    if result.projection is not None:
        print("\n".join(format_projection_lines(result.projection)))
    return 1 if result.failed else 0


def _submit_simple(gateway: SessionGateway, *, session_id: str, kind: CommandKind) -> int:
    directive = "/pause" if kind == CommandKind.PAUSE else "/resume"
    result = _submit_and_wait(
        InteractiveSession(gateway, session_id=session_id),
        directive,
    )
    if result.message:
        print(result.message)
    if result.projection is not None:
        # Show this command's events, or suppress prior history when it emitted none.
        after_sequence = (
            result.events[0].sequence - 1 if result.events else result.projection.revision
        )
        lines = format_projection_lines(
            result.projection,
            after_sequence=after_sequence,
        )
        print(
            "\n".join(lines)
            if lines
            else ("Session paused" if kind == CommandKind.PAUSE else "Session resumed")
        )
    return 1 if result.failed else 0


def _interactive_chat(gateway: SessionGateway, *, session_id: str, plain: bool = False) -> int:
    session = InteractiveSession(gateway, session_id=session_id)
    if not plain and _supports_full_screen_terminal():
        from heartwood.cli._tui import run_terminal

        return run_terminal(session)
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
        if result.projection is not None:
            after_sequence = (
                None
                if result.replace_transcript or not result.events
                else _live_projection_start(result.events)
            )
            print(
                "\n".join(
                    format_projection_lines(
                        result.projection,
                        after_sequence=after_sequence,
                    )
                )
            )


def _live_projection_start(events: tuple[SessionEvent, ...]) -> int:
    """Skip a terminal-echoed user message while retaining new agent output."""
    user_sequences = [
        event.sequence
        for event in events
        if _event_kind(event) == EventKind.USER_MESSAGE_RECORDED.value
    ]
    return max(user_sequences) if user_sequences else events[0].sequence - 1


def _submit_with_progress(
    session: InteractiveSession,
    line: str,
    *,
    update_interval: float = 15,
) -> InteractionResult:
    """Submit one blocking line-mode turn while reporting honest elapsed time."""
    return _run_with_progress(
        lambda: _submit_and_wait(session, line),
        activity=interaction_activity(line),
        update_interval=update_interval,
    )


def _submit_and_wait(session: InteractiveSession, line: str) -> InteractionResult:
    result = session.submit(line)
    projection = result.projection
    if projection is not None and projection.lifecycle.status == "running":
        projection = session.wait_until_stable()
    return InteractionResult(
        events=result.events,
        projection=projection,
        message=result.message,
        exit_requested=result.exit_requested,
        error=result.error,
        replace_transcript=result.replace_transcript,
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
    animated = sys.stderr.isatty() and "NO_COLOR" not in os.environ
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
                print(
                    f"\r\033[2K{label}{marker}{suffix}",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"{activity.waiting_label} ({elapsed}s elapsed). {activity.guidance}",
                    file=sys.stderr,
                    flush=True,
                )

    if animated:
        print(f"{activity.label}{frames[0]}", end="", file=sys.stderr, flush=True)
    else:
        print(f"{activity.label}...", file=sys.stderr, flush=True)
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
            print("\r\033[2K", end="", file=sys.stderr, flush=True)


def _supports_full_screen_terminal() -> bool:
    return (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and os.environ.get("TERM", "").lower() not in {"", "dumb"}
    )


def _handle_replay(gateway: SessionGateway, *, session_id: str) -> int:
    projection = gateway.persisted_session_projection(session_id=session_id)
    lines = format_projection_lines(projection)
    print("\n".join(lines) if lines else "No session events recorded.")
    return 0


def _handle_audit_export(
    gateway: SessionGateway,
    *,
    session_id: str,
    output: Path | None,
) -> int:
    events = gateway.handle(_command(session_id=session_id, kind=CommandKind.AUDIT_EXPORT)).events
    export_path = Path(str(events[-1].payload["path"]))
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(export_path, output)
    print(f"Audit export: {output or export_path}")
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
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


def _command(
    *,
    session_id: str,
    kind: CommandKind,
    payload: dict[str, JsonValue] | None = None,
) -> SessionCommand:
    return SessionCommand(
        command_id=new_command_id(session_id, kind),
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
