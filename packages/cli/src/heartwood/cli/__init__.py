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
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

import uvicorn

from heartwood.adapters import INGRESS_MODES
from heartwood.adapters.platform import select_platform_adapter
from heartwood.cli._audit import (
    _handle_audit_checkpoint,
    _handle_audit_checkpoint_verification,
    _handle_audit_export,
    _handle_audit_signer,
    _handle_audit_verify,
    _handle_local_signer_init,
    _handle_local_signer_serve,
    configure_audit,
    configure_signer,
)
from heartwood.cli._brand import lockup
from heartwood.cli._experiments import configure_experiments, handle_experiments
from heartwood.cli._interactive import (
    InteractionResult,
    InteractiveSession,
    command_help,
    format_action_settings,
    format_projection_lines,
    format_specialist_settings,
    interaction_activity,
)
from heartwood.cli._launch import LaunchOptions, run_launch
from heartwood.cli._models import _handle_models, configure_models
from heartwood.cli._progress import _run_with_progress
from heartwood.cli._setup import (
    _STARTUP_ACTIVITY,
    _configure_setup,
    _format_readiness,
    _handle_setup,
    configure_setup,
)
from heartwood.cli._skills import _handle_skills, configure_skills
from heartwood.cli._usage import configure_usage, handle_usage
from heartwood.cli._workspace_presentation import (
    format_workspace_changes,
    format_workspace_diff,
    format_workspace_file,
    format_workspace_tree,
)
from heartwood.gateway import (
    ACTION_MODE_OPTIONS,
    DEFAULT_SESSION_ID,
    ActionSettingsError,
    AuditCheckpointError,
    AuditIntegrityError,
    CheckpointSignerError,
    CommandConflictError,
    GatewayAsgiApp,
    IngressConfigurationError,
    IngressPolicy,
    InterfaceKind,
    LaunchCapability,
    NativeLockUnavailableError,
    ProjectContext,
    ProjectOwnershipError,
    ProjectStateError,
    SessionGateway,
    SessionOwnershipError,
    SessionRecoveryError,
    StartupPlan,
    WorkspaceInspectionError,
    diagnostic_for,
    discover_checkpoint_signer_registry,
    load_checkpoint_signer_registry,
    load_project_deployment,
)
from heartwood.session import CommandKind, EventKind, SessionEvent, validate_session_id

__all__ = ["__version__", "main"]

__version__ = "0.5.0"

_PROG = "heartwood"


def _bundled_path(relative: Path) -> Path:
    module_dir = Path(__file__).resolve().parent
    packaged = module_dir / "_bundled" / relative
    if packaged.exists():
        return packaged
    if len(module_dir.parents) > 4:
        source_root = module_dir.parents[4]
        if source_root / "packages/cli/src/heartwood/cli" == module_dir:
            return source_root / relative
    return packaged


_DEFAULT_WEB_ROOT = _bundled_path(Path("packages") / "webui" / "dist")
_DEFAULT_FIXTURE_ROOT = _bundled_path(Path("fixtures") / "synthetic")


_REVIEW_MODE_CHOICES = tuple(option.mode for option in ACTION_MODE_OPTIONS)


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
    parser.add_argument(
        "--review-mode",
        choices=_REVIEW_MODE_CHOICES,
        help="Select the action-review mode for this launch; policy limits still apply.",
    )
    parser.add_argument("--prompt", "-p", help="Submit one task and exit.")
    parser.add_argument("--prompt-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8767, help="Browser interface port.")
    parser.add_argument(
        "--host-loopback-publication",
        action="store_true",
        help=(
            "Assert that a wildcard container bind is published only on the host's "
            "loopback interface."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    doctor = subparsers.add_parser("doctor", help="Inspect environment and setup readiness.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable diagnostics.")
    doctor.add_argument(
        "--deployment",
        type=Path,
        help="Absolute operator manifest whose effective policy to report for this project.",
    )
    configure_experiments(
        subparsers.add_parser(
            "experiments", help="Record analyses and inspect experiment provenance."
        )
    )
    configure_setup(
        subparsers.add_parser("setup", help="Configure a model route and conservative policy.")
    )
    configure_usage(
        subparsers.add_parser("usage", help="Summarize project use without session content.")
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

    subparsers.add_parser(
        "actions", help="Show the action-review mode and each mode this launch can use."
    )

    configure_models(
        subparsers.add_parser(
            "models", help="Choose a model connection or manage advanced profiles."
        )
    )

    configure_skills(
        subparsers.add_parser("skills", help="Browse and manage verified Agent Skills.")
    )

    subparsers.add_parser(
        "specialists",
        help="List bounded research specialists available to the agent.",
    )

    files = subparsers.add_parser("files", help="Inspect bounded project files.")
    file_subparsers = files.add_subparsers(dest="files_command", metavar="<files-command>")
    file_list = file_subparsers.add_parser("list", help="List the bounded project tree.")
    file_list.add_argument("path", nargs="?", default=".")
    file_list.add_argument("--depth", type=int)
    file_show = file_subparsers.add_parser("show", help="Show one bounded UTF-8 project file.")
    file_show.add_argument("path")

    changes = subparsers.add_parser("changes", help="Inspect changed project files.")
    changes.add_argument(
        "path",
        nargs="?",
        help="Show one changed file; omit to list changed paths.",
    )

    configure_audit(subparsers.add_parser("audit", help="Audit-log operations."))

    configure_signer(
        subparsers.add_parser(
            "signer",
            help="Manage the explicit local checkpoint signer fallback.",
        )
    )

    project_operations = subparsers.add_parser("project", help="Explicit project state operations.")
    project_commands = project_operations.add_subparsers(dest="project_command", required=True)
    migrate = project_commands.add_parser("migrate", help="Move local state to protected storage.")
    migrate.add_argument("--deployment", type=Path, required=True)
    migrate.add_argument("--backup", type=Path, required=True)
    recover = project_commands.add_parser("recover-migration", help="Recover a state migration.")
    recover.add_argument("--deployment", type=Path, required=True)
    recover.add_argument("--rollback", action="store_true")

    gateway = subparsers.add_parser("gateway", help="Advanced gateway operations.")
    gateway_subparsers = gateway.add_subparsers(dest="gateway_command", metavar="<gateway-command>")
    gateway_serve = gateway_subparsers.add_parser(
        "serve", help="Serve the gateway and packaged browser interface."
    )
    gateway_serve.add_argument("--host", default="127.0.0.1", help="Gateway bind host.")
    gateway_serve.add_argument("--port", type=int, default=8767, help="Gateway bind port.")
    gateway_serve.add_argument("--web-root", type=Path, default=_DEFAULT_WEB_ROOT)
    gateway_serve.add_argument(
        "--capability-fd",
        type=int,
        help="Consume an inherited file or pipe descriptor containing the operator capability.",
    )
    gateway_serve.add_argument(
        "--review-mode",
        choices=_REVIEW_MODE_CHOICES,
        default=argparse.SUPPRESS,
        help="Select the action-review mode at start; policy limits still apply.",
    )
    gateway_serve.add_argument(
        "--deployment",
        type=Path,
        help="Absolute operator manifest binding this project to external state and model policy.",
    )
    gateway_serve.add_argument(
        "--ingress-mode",
        choices=INGRESS_MODES,
        help="Explicit network route to the gateway; defaults to the platform capability.",
    )
    gateway_serve.add_argument(
        "--host-loopback-publication",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "Assert that a wildcard container bind is published only on the host's "
            "loopback interface."
        ),
    )
    gateway_serve.add_argument(
        "--public-origin",
        help="Exact browser-visible origin for a proxy route.",
    )
    gateway_serve.add_argument(
        "--base-path",
        default="/",
        help="Exact browser-visible gateway base path.",
    )
    gateway_serve.add_argument(
        "--trusted-proxy-source",
        action="append",
        default=[],
        help="Trusted proxy IP or CIDR; repeat for multiple sources.",
    )
    gateway_serve.add_argument(
        "--trusted-identity-header",
        help="Optional header carrying a trusted proxy identity.",
    )
    gateway_serve.add_argument(
        "--trusted-identity",
        help="Exact trusted proxy identity value.",
    )
    gateway_serve.add_argument(
        "--proxy-strips-prefix",
        action="store_true",
        help="Declare that a trusted proxy removes the external base path.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``heartwood`` and return a process exit code."""
    try:
        return _main(argv)
    except (
        CommandConflictError,
        NativeLockUnavailableError,
        SessionOwnershipError,
        SessionRecoveryError,
    ) as error:
        print(f"Session unavailable: {error}", file=sys.stderr)
        return 75
    except WorkspaceInspectionError as error:
        print(f"Project files unavailable: {error}", file=sys.stderr)
        return 64
    except ProjectOwnershipError as error:
        print(f"Project unavailable: {error}", file=sys.stderr)
        return 75
    except (AuditCheckpointError, AuditIntegrityError, CheckpointSignerError) as error:
        print(f"Audit operation failed: {error}", file=sys.stderr)
        return 65
    except ProjectStateError as error:
        print(f"Project unavailable: {error}", file=sys.stderr)
        return 64
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


def _main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    project = ProjectContext.current()
    if args.command == "project":
        from heartwood.gateway._project_migration import (
            migrate_project_state,
            recover_project_migration,
        )

        try:
            if args.project_command == "migrate":
                journal = migrate_project_state(
                    deployment=args.deployment, workspace=project.root, backup=args.backup
                )
            else:
                journal = recover_project_migration(
                    deployment=args.deployment, workspace=project.root, rollback=args.rollback
                )
        except (OSError, ValueError) as error:
            print(f"Project migration stopped: {error}", file=sys.stderr)
            return 64
        print(f"Migration evidence: {journal}")
        return 0
    if args.port is not None and (args.port < 1 or args.port > 65_535):
        parser.error("--port must be between 1 and 65535")
    if args.plain and args.interface != "terminal":
        parser.error("--plain can be used only with --interface terminal")
    if args.review_mode is not None and not (
        args.command is None
        or (args.command == "gateway" and args.gateway_command == "serve")
        or (args.command == "runtime" and args.runtime_command == "start")
    ):
        parser.error("--review-mode applies only to commands that start an interface")
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
            ingress_mode=args.ingress_mode,
            public_origin=args.public_origin,
            trusted_proxy_sources=args.trusted_proxy_source,
            trusted_identity_header=args.trusted_identity_header,
            trusted_identity=args.trusted_identity,
            proxy_strips_prefix=args.proxy_strips_prefix,
            host_loopback_publication=args.host_loopback_publication,
            deployment_path=args.deployment,
            capability_descriptor=args.capability_fd,
            review_mode=args.review_mode,
        )
    if args.command == "doctor":
        return _handle_doctor(project=project, as_json=args.json, deployment_path=args.deployment)
    if args.command == "experiments":
        return handle_experiments(args, project=project)
    if args.command == "usage":
        return handle_usage(args, project=project)
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
    if args.command == "signer" and args.signer_command == "init-local":
        return _handle_local_signer_init(
            project=project,
            directory=args.directory,
            profile_id=args.profile,
            key_version=args.key_version,
        )
    if args.command == "signer" and args.signer_command == "serve-local":
        registry = (
            discover_checkpoint_signer_registry(os.environ)
            if args.registry is None
            else load_checkpoint_signer_registry(args.registry)
        )
        return _handle_local_signer_serve(
            project=project,
            registry=registry,
            profile_id=args.profile,
            private_key=args.private_key,
        )
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
                host_loopback_publication=args.host_loopback_publication,
                review_mode=args.review_mode,
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
                host_loopback_publication=args.host_loopback_publication,
                review_mode=args.review_mode,
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
            return _handle_actions(parser, gateway)
        if args.command == "skills":
            return _handle_skills(parser, gateway, args)
        if args.command == "specialists":
            print(format_specialist_settings(gateway.specialist_settings()))
            return 0
        if args.command == "files":
            return _handle_files(parser, gateway, args)
        if args.command == "changes":
            return _handle_changes(gateway, session_id=args.session_id, path=args.path)
        if args.command is None:
            _select_launch_review_mode(gateway, args.review_mode)
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
        if args.command == "audit" and args.audit_command == "verify":
            return _handle_audit_verify(gateway, session_id=args.session_id)
        if args.command == "audit" and args.audit_command == "signer":
            return _handle_audit_signer(gateway, command=args.audit_signer_command, args=args)
        if args.command == "audit" and args.audit_command == "checkpoint":
            return _handle_audit_checkpoint(
                gateway,
                session_id=args.session_id,
                output=args.output,
                deployment_id=args.deployment_id,
                retention_policy_id=args.retention_policy,
                retain_until=args.retain_until,
                include_experiments=args.include_experiments,
            )
        if args.command == "audit" and args.audit_command == "verify-checkpoint":
            return _handle_audit_checkpoint_verification(
                gateway,
                bundle=args.bundle,
                public_key=args.public_key,
            )
        parser.print_help()
        return 0
    finally:
        gateway.stop()


def _handle_doctor(
    *, project: ProjectContext, as_json: bool, deployment_path: Path | None = None
) -> int:
    # Inspection reads the binding and its policy; it launches no worker or evidence client.
    gateway = (
        SessionGateway(
            deployment=load_project_deployment(deployment_path, project_root=project.root)
        )
        if deployment_path is not None
        else SessionGateway(project=project)
    )
    readiness = gateway.deployment_readiness()
    gateway.stop()
    print(json.dumps(readiness.safe_dict(), indent=2) if as_json else _format_readiness(readiness))
    return 1 if readiness.state == "recovery-required" else 0


def _format_startup_plan(startup: StartupPlan) -> str:
    plan = startup.safe_dict()
    capabilities = plan["capabilities"]
    if not isinstance(capabilities, dict):  # pragma: no cover - typed serialization invariant
        raise TypeError("startup capabilities must be an object")
    return "\n".join(
        (
            lockup(sys.stdout),
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


def _select_launch_review_mode(gateway: SessionGateway, mode: str | None) -> None:
    """Apply --review-mode as the researcher's selection, or stop with the gateway's reason."""
    if mode is None:
        return
    try:
        gateway.select_action_review_mode(mode)
    except ActionSettingsError as error:
        raise SystemExit(f"--review-mode {mode} cannot be used: {error}") from error


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
        host_loopback_publication=args.host_loopback_publication,
        startup_timeout=getattr(args, "startup_timeout", 600),
        review_mode=args.review_mode,
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


def _handle_actions(parser: argparse.ArgumentParser, gateway: SessionGateway) -> int:
    """Show the action-review mode; it is selected at launch or in a running interface."""
    try:
        settings = gateway.action_settings()
    except ActionSettingsError as error:
        parser.error(str(error))
    print(format_action_settings(settings))
    return 0


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
    displayed_group_id: str | None = None
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
        if line in {"/allow", "/reject"}:
            # A bare decision applies to the action set this terminal printed.
            if displayed_group_id is None:
                projection = session.replay()
                if (approval := projection.pending_approval) is not None:
                    displayed_group_id = approval.group_id
                    print("\n".join(format_projection_lines(projection)))
                    print("Review the action set above, then repeat the decision.")
                    continue
            else:
                line = f"{line} {shlex.quote(displayed_group_id)}"
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
            approval = result.projection.pending_approval
            displayed_group_id = None if approval is None else approval.group_id
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


def _handle_files(
    parser: argparse.ArgumentParser,
    gateway: SessionGateway,
    args: argparse.Namespace,
) -> int:
    if args.files_command == "list":
        if args.depth is not None and args.depth < 1:
            parser.error("--depth must be positive")
        tree = gateway.workspace_tree(path=args.path, depth=args.depth)
        print("\n".join(format_workspace_tree(tree)))
        return 0
    if args.files_command == "show":
        file = gateway.workspace_file(path=args.path)
        print("\n".join(format_workspace_file(file)))
        return 0 if file["status"] in {"available", "truncated"} else 1
    parser.error("files requires list or show")
    raise AssertionError("argparse returned after reporting an invalid files command")


def _handle_changes(
    gateway: SessionGateway,
    *,
    session_id: str,
    path: str | None,
) -> int:
    if path is None:
        changes = gateway.workspace_changes(session_id=session_id)
        print("\n".join(format_workspace_changes(changes)))
        return 0 if changes["status"] not in {"unavailable", "unsupported"} else 1
    diff = gateway.workspace_diff(session_id=session_id, path=path)
    print("\n".join(format_workspace_diff(diff)))
    return 0 if diff["status"] in {"available", "non-git", "truncated"} else 1


def _handle_serve(
    *,
    project: ProjectContext,
    host: str,
    port: int,
    web_root: Path,
    base_path: str,
    ingress_mode: str | None = None,
    public_origin: str | None = None,
    trusted_proxy_sources: Sequence[str] = (),
    trusted_identity_header: str | None = None,
    trusted_identity: str | None = None,
    proxy_strips_prefix: bool = False,
    host_loopback_publication: bool = False,
    deployment_path: Path | None = None,
    capability_descriptor: int | None = None,
    review_mode: str | None = None,
) -> int:
    deployment = (
        load_project_deployment(deployment_path, project_root=project.root)
        if deployment_path is not None
        else None
    )
    if deployment is not None:
        project = deployment.project
    if not web_root.exists():
        msg = f"web UI assets not found: {web_root}"
        raise SystemExit(msg)
    capabilities = select_platform_adapter(os.environ).capabilities()
    selected_ingress_mode = ingress_mode or capabilities.default_ingress_mode
    diagnostic = diagnostic_for("gateway-ingress")
    if selected_ingress_mode not in capabilities.ingress_modes:
        raise SystemExit(
            f"{diagnostic.code}: {capabilities.display_name} does not allow "
            f"{selected_ingress_mode} gateway ingress"
        )
    try:
        ingress = IngressPolicy.create(
            mode=selected_ingress_mode,
            bind_host=host,
            bind_port=port,
            external_origin=public_origin,
            external_base_path=base_path,
            prefix_handling="strip" if proxy_strips_prefix else None,
            trusted_proxy_sources=trusted_proxy_sources,
            trusted_identity_header=trusted_identity_header,
            trusted_identity=trusted_identity,
            host_loopback_publication=host_loopback_publication,
        )
        if "HEARTWOOD_GATEWAY_CAPABILITY" in os.environ:
            raise IngressConfigurationError(
                "environment capability delivery is unsupported; use --capability-fd"
            )
        access = (
            LaunchCapability.from_descriptor(capability_descriptor)
            if capability_descriptor is not None
            else LaunchCapability.generate()
        )
    except IngressConfigurationError as error:
        raise SystemExit(f"{diagnostic.code}: {error}") from error
    with ExitStack() as resources:
        checkpoint_evidence_factory = None
        if deployment is not None and deployment.evidence is not None:
            from heartwood.gateway.checkpoint_deployment import deployment_checkpoint_evidence

            checkpoint_evidence_factory = resources.enter_context(
                deployment_checkpoint_evidence(deployment.evidence, project)
            )
        worker_plan_factory = worker_reconciler = None
        if deployment is not None and deployment.worker is not None:
            from heartwood.core_adapter.worker_attempts import WorkerAttemptError
            from heartwood.gateway.worker_control import WorkerGatewayVersionError
            from heartwood.gateway.worker_native import (
                native_worker_plan_factory,
                native_worker_reconciler,
                require_declared_gateway,
            )

            worker_plan_factory = native_worker_plan_factory(deployment.worker, project)
            try:
                require_declared_gateway(deployment.worker, project)
            except WorkerGatewayVersionError as error:
                code = diagnostic_for("worker-gateway-version").code
                raise SystemExit(f"{code}: {error}") from error
            except (WorkerAttemptError, ProjectStateError) as error:
                raise SystemExit(f"Managed worker control is unavailable: {error}") from error
            worker_reconciler = native_worker_reconciler(deployment.worker, project)
        gateway = (
            SessionGateway(
                deployment=deployment,
                worker_plan_factory=worker_plan_factory,
                worker_reconciler=worker_reconciler,
                checkpoint_evidence_factory=checkpoint_evidence_factory,
            )
            if deployment is not None
            else SessionGateway(project=project)
        )
        _select_launch_review_mode(gateway, review_mode)
        for session_id, check in gateway.reconcile_workers().items():
            print(
                f"{diagnostic_for(check).code}: the worker attempt of session {session_id} "
                "remains fenced; "
                + (
                    "it ran on another sandbox control plane or container runtime."
                    if check == "worker-control-plane"
                    else "revocation is retried while the gateway runs."
                ),
                file=sys.stderr,
            )
        app = GatewayAsgiApp(gateway, static_dir=web_root, ingress=ingress, access=access)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            proxy_headers=False,
            access_log=False,
            ws_max_size=8192,
        )
        with config.bind_socket() as listener:
            listener.set_inheritable(False)
            listener.listen(config.backlog)
            launch_url = access.launch_url(ingress)
            if launch_url is not None:
                print(f"Open Heartwood with this one-time link:\n  {launch_url}\n", flush=True)
            uvicorn.Server(config).run(sockets=[listener])
    return 0


def _session_id_argument(value: str) -> str:
    try:
        return validate_session_id(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _event_kind(event: SessionEvent) -> str:
    return str(event.kind)
