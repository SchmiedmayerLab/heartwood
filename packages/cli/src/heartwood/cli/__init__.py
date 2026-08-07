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
from typing import cast
from urllib.parse import urlsplit

import uvicorn

from heartwood.adapters import INGRESS_MODES
from heartwood.adapters.platform import select_platform_adapter
from heartwood.cli._interactive import (
    InteractionActivity,
    InteractionResult,
    InteractiveSession,
    command_help,
    format_action_settings,
    format_projection_lines,
    format_specialist_settings,
    interaction_activity,
)
from heartwood.cli._launch import LaunchOptions, run_launch
from heartwood.cli._workspace_presentation import (
    format_workspace_changes,
    format_workspace_diff,
    format_workspace_file,
    format_workspace_tree,
)
from heartwood.gateway import (
    ACTION_MODE_OPTIONS,
    BUILT_IN_MODEL_CONNECTIONS,
    DEFAULT_SESSION_ID,
    MODEL_SOURCE_OPTIONS,
    ActionSettingsError,
    AuditCheckpointError,
    AuditIntegrityError,
    CheckpointSignerError,
    CheckpointSignerRegistry,
    CommandConflictError,
    CredentialStoreError,
    DeploymentReadiness,
    GatewayAsgiApp,
    IngressConfigurationError,
    IngressPolicy,
    InterfaceKind,
    LocalCheckpointSignerApp,
    LocalEd25519CheckpointSigner,
    ModelArtifactError,
    ModelCatalogError,
    ModelProfile,
    ModelRepositoryError,
    ModelSettingsError,
    ModelSnapshotError,
    ModelTransferError,
    NativeLockUnavailableError,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    ProjectStateError,
    SessionGateway,
    SessionOwnershipError,
    SessionRecoveryError,
    SkillSettingsError,
    StartupPlan,
    WorkspaceInspectionError,
    action_mode_label,
    checkpoint_public_key_fingerprint,
    custom_model_connection_requires_token,
    diagnostic_for,
    discover_checkpoint_signer_registry,
    initialize_local_checkpoint_signer,
    inspect_deployment,
    load_checkpoint_signer_registry,
    model_source_options,
    user_checkpoint_signer_registry_path,
)
from heartwood.schemas import (
    ModelArtifactsResponse,
    ModelCatalogResponse,
    ModelRepositoryPlanResponse,
    ModelSettingsResponse,
    ModelTransferPlanResponse,
    ModelTransferResponse,
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

__version__ = "0.3.0-beta.4"

_PROG = "heartwood"


def _sha256_argument(value: str) -> str:
    digest = value.removeprefix("sha256:").lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise argparse.ArgumentTypeError("expected a complete SHA-256 digest")
    return digest


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
_MODEL_PREPARATION_ACTIVITY = InteractionActivity(
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
        "import", help="Import a Heartwood bundle or an existing model artifact."
    )
    import_model.add_argument("path", type=Path, help="Existing model file or directory.")
    import_model.add_argument(
        "--source",
        help="Upstream Hugging Face owner/model identifier.",
    )
    import_model.add_argument(
        "--revision",
        help="Immutable upstream commit hash.",
    )
    import_model.add_argument(
        "--license",
        dest="license_posture",
        help="Upstream license identifier or review note.",
    )
    import_model.add_argument(
        "--context-window",
        type=int,
        default=32_768,
        help="Maximum model context supported by this artifact.",
    )
    import_model.add_argument(
        "--approve-license",
        action="store_true",
        help="Confirm the displayed bundle license without an interactive prompt.",
    )
    export_model = model_subparsers.add_parser(
        "export", help="Export the selected model as a verified portable bundle."
    )
    export_model.add_argument("path", type=Path, help="New bundle output path.")
    inspect_bundle = model_subparsers.add_parser(
        "inspect-bundle", help="Inspect a portable model bundle without importing it."
    )
    inspect_bundle.add_argument("path", type=Path, help="Existing model bundle path.")

    skills = subparsers.add_parser("skills", help="Browse and manage verified Agent Skills.")
    skill_subparsers = skills.add_subparsers(dest="skills_command", metavar="<skills-command>")
    skill_subparsers.add_parser("list", help="List available and active Skills.")
    refresh_skills = skill_subparsers.add_parser(
        "refresh", help="Refresh deployment-approved signed Skill sources."
    )
    refresh_skills.add_argument("--source", dest="source_id")
    inspect = skill_subparsers.add_parser("inspect", help="Review one Skill from a signed source.")
    inspect.add_argument("name")
    inspect.add_argument("--source", dest="source_id")
    install = skill_subparsers.add_parser(
        "install", help="Install one signed Skill after explicit review."
    )
    install.add_argument("name")
    install.add_argument("--source", dest="source_id")
    install.add_argument(
        "--approve",
        action="store_true",
        help="Approve the digest supplied with --expected-tree-sha256.",
    )
    install.add_argument(
        "--expected-tree-sha256",
        type=_sha256_argument,
        metavar="SHA256",
        help="Exact content digest shown by `skills inspect`; required with --approve.",
    )
    inspect_local = skill_subparsers.add_parser(
        "inspect-local", help="Inspect an advanced local, unreviewed Agent Skill."
    )
    inspect_local.add_argument("source", type=Path)
    install_local = skill_subparsers.add_parser(
        "install-local", help="Install an advanced local, unreviewed Agent Skill."
    )
    install_local.add_argument("source", type=Path)
    install_local.add_argument(
        "--approve",
        action="store_true",
        help="Approve the digest supplied with --expected-tree-sha256.",
    )
    install_local.add_argument(
        "--expected-tree-sha256",
        type=_sha256_argument,
        metavar="SHA256",
        help="Exact content digest shown by `skills inspect-local`; required with --approve.",
    )
    remove_skill = skill_subparsers.add_parser("remove", help="Remove an installed extension.")
    remove_skill.add_argument("name")

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

    audit = subparsers.add_parser("audit", help="Audit-log operations.")
    audit_subparsers = audit.add_subparsers(dest="audit_command", metavar="<audit-command>")
    audit_export = audit_subparsers.add_parser("export", help="Export scrubbed audit JSONL.")
    audit_export.add_argument("--output", type=Path, help="Optional copy destination.")
    audit_subparsers.add_parser("verify", help="Fully verify the current session audit history.")
    audit_signer = audit_subparsers.add_parser(
        "signer",
        help="Inspect or select deployment-approved checkpoint signing.",
    )
    audit_signer_subparsers = audit_signer.add_subparsers(
        dest="audit_signer_command",
        metavar="<signer-command>",
    )
    audit_signer_subparsers.add_parser("list", help="List approved signer profiles.")
    audit_signer_select = audit_signer_subparsers.add_parser(
        "select",
        help="Select an approved signer profile for this project.",
    )
    audit_signer_select.add_argument("profile_id")
    audit_signer_subparsers.add_parser(
        "default",
        help="Use the deployment default signer for this project.",
    )
    audit_checkpoint = audit_subparsers.add_parser(
        "checkpoint",
        help="Create a signed authoritative audit bundle outside the project.",
    )
    audit_checkpoint.add_argument("--output", type=Path, required=True)
    audit_checkpoint.add_argument("--deployment-id", required=True)
    audit_checkpoint.add_argument("--retention-policy", required=True)
    audit_checkpoint.add_argument(
        "--retain-until",
        required=True,
        help="Retention end date in YYYY-MM-DD format.",
    )
    audit_checkpoint_verify = audit_subparsers.add_parser(
        "verify-checkpoint",
        help="Verify a signed audit bundle with a trusted public key.",
    )
    audit_checkpoint_verify.add_argument("bundle", type=Path)
    audit_checkpoint_verify.add_argument(
        "--public-key",
        type=Path,
        help="Advanced: independently trusted key; defaults to the active signer profile.",
    )

    signer = subparsers.add_parser(
        "signer",
        help="Manage the explicit local checkpoint signer fallback.",
    )
    signer_subparsers = signer.add_subparsers(dest="signer_command", metavar="<signer-command>")
    signer_init = signer_subparsers.add_parser(
        "init-local",
        help="Create local development signer material outside the project.",
    )
    signer_init.add_argument("--directory", type=Path)
    signer_init.add_argument("--profile", default="local-development")
    signer_init.add_argument("--key-version", default="v1")
    signer_init.add_argument("--port", type=int, default=8771)
    signer_serve = signer_subparsers.add_parser(
        "serve-local",
        help="Run an initialized signer on the loopback interface.",
    )
    signer_serve.add_argument("--registry", type=Path)
    signer_serve.add_argument("--profile")
    signer_serve.add_argument("--private-key", type=Path)
    signer_serve.add_argument("--host", choices=("127.0.0.1", "::1"))
    signer_serve.add_argument("--port", type=int)

    gateway = subparsers.add_parser("gateway", help="Advanced gateway operations.")
    gateway_subparsers = gateway.add_subparsers(dest="gateway_command", metavar="<gateway-command>")
    gateway_serve = gateway_subparsers.add_parser(
        "serve", help="Serve the gateway and packaged browser interface."
    )
    gateway_serve.add_argument("--host", default="127.0.0.1", help="Gateway bind host.")
    gateway_serve.add_argument("--port", type=int, default=8767, help="Gateway bind port.")
    gateway_serve.add_argument("--web-root", type=Path, default=_DEFAULT_WEB_ROOT)
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
    if args.port is not None and (args.port < 1 or args.port > 65_535):
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
            ingress_mode=args.ingress_mode,
            public_origin=args.public_origin,
            trusted_proxy_sources=args.trusted_proxy_source,
            trusted_identity_header=args.trusted_identity_header,
            trusted_identity=args.trusted_identity,
            proxy_strips_prefix=args.proxy_strips_prefix,
            host_loopback_publication=args.host_loopback_publication,
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
    if args.command == "signer" and args.signer_command == "init-local":
        return _handle_local_signer_init(
            project=project,
            directory=args.directory,
            profile_id=args.profile,
            key_version=args.key_version,
            port=args.port,
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
            host=args.host,
            port=args.port,
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
        if args.command == "specialists":
            print(format_specialist_settings(gateway.specialist_settings()))
            return 0
        if args.command == "files":
            return _handle_files(parser, gateway, args)
        if args.command == "changes":
            return _handle_changes(gateway, session_id=args.session_id, path=args.path)
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
        host_loopback_publication=args.host_loopback_publication,
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
            activity=_MODEL_PREPARATION_ACTIVITY,
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
            activity=_MODEL_PREPARATION_ACTIVITY,
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
        if command == "inspect-bundle":
            print(_format_model_transfer_plan(gateway.inspect_local_model_bundle(args.path)))
            return 0
        if command == "export":
            transfer = gateway.export_local_model(args.path)
            completed = _wait_for_model_transfer(gateway, transfer)
            print(f"Model bundle is ready: {completed['result_path']}")
            print("Transfer the bundle through an approved channel, then import it in Heartwood.")
            return 0
        if command == "import":
            raw_fields = (args.source, args.revision, args.license_posture)
            if not any(raw_fields):
                plan = gateway.inspect_local_model_bundle(args.path)
                print(_format_model_transfer_plan(plan))
                if not args.approve_license:
                    try:
                        approved = input(
                            "Import this model and accept the displayed license? [y/N]: "
                        )
                    except EOFError as error:
                        raise ModelTransferError(
                            "model bundle import approval was cancelled"
                        ) from error
                    if approved.strip().lower() not in {"y", "yes"}:
                        raise ModelTransferError("model bundle import was not approved")
                transfer = gateway.import_local_model_bundle(
                    args.path,
                    approved=True,
                    manifest_sha256=plan["manifest_sha256"],
                )
                completed = _wait_for_model_transfer(gateway, transfer)
                print(f"{completed['label']} is ready in this project.")
                print(f"Location: {completed['result_path']}")
                print("Run `heartwood` to use it.")
                return 0
            if not all(raw_fields):
                parser.error(
                    "raw model imports require --source, --revision, and --license together"
                )
            if args.approve_license:
                parser.error("--approve-license applies only to Heartwood model bundles")
            imported = _run_with_progress(
                lambda: gateway.import_local_model(
                    args.path,
                    source_repository=cast(str, args.source),
                    source_revision=cast(str, args.revision),
                    license_posture=cast(str, args.license_posture),
                    context_window=args.context_window,
                ),
                activity=_MODEL_PREPARATION_ACTIVITY,
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
                    activity=_MODEL_PREPARATION_ACTIVITY,
                )
            else:
                path = _run_with_progress(
                    lambda: gateway.download_custom_local_model_now(
                        args.model,
                        revision=args.revision,
                    ),
                    activity=_MODEL_PREPARATION_ACTIVITY,
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
        ModelTransferError,
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
    lines = [
        "Heartwood models",
        "",
        f"Credential isolation: {settings['credential_isolation']['summary']}",
        "",
        "Connections:",
    ]
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
            f"Credential isolation: {validation['credential_isolation']['summary']}",
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


def _format_model_transfer_plan(plan: ModelTransferPlanResponse) -> str:
    model = plan["model"]
    runtime = "NVIDIA GPU" if model["runtime"] == "vllm" else "CPU"
    lines = [
        "Heartwood model bundle",
        "",
        f"Model: {model['label']}",
        f"Repository: {model['source_repository']}",
        f"Revision: {model['source_revision']}",
        f"License: {model['license_posture']}",
        f"Runtime: {runtime}",
        f"Model files: {_format_transfer_bytes(model['size_bytes'])}",
        "Bundle: "
        f"{_format_transfer_bytes(plan['bundle_size_bytes'])} in {plan['file_count']} files",
        f"Context capacity: up to {model['context_window']:,} tokens",
    ]
    if plan["warnings"]:
        lines.extend(("", "Review before import:"))
        lines.extend(f"  - {warning}" for warning in plan["warnings"])
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
        if command == "refresh":
            settings = _run_with_progress(
                lambda: gateway.refresh_skills(args.source_id),
                activity=InteractionActivity(
                    label="Refreshing verified Skills",
                    waiting_label="Still verifying Skill sources",
                    guidance="Network and offline source response times can vary.",
                ),
            )
            print(_format_skill_settings(settings))
            return 0
        if command == "inspect":
            summary = _run_with_progress(
                lambda: gateway.inspect_skill(args.name, source_id=args.source_id),
                activity=InteractionActivity(
                    label="Verifying the Skill catalog",
                    waiting_label="Still verifying the Skill catalog",
                    guidance="Heartwood is checking signed metadata and immutable digests.",
                ),
            )
            print(_format_skill_summary(summary))
            return 0
        if command == "install":
            expected_tree_sha256 = args.expected_tree_sha256
            if args.approve:
                if expected_tree_sha256 is None:
                    parser.error(
                        "--approve requires --expected-tree-sha256 with the digest shown by "
                        "`heartwood skills inspect`"
                    )
            else:
                if expected_tree_sha256 is not None:
                    parser.error("--expected-tree-sha256 requires --approve")
                summary = _run_with_progress(
                    lambda: gateway.inspect_skill(args.name, source_id=args.source_id),
                    activity=InteractionActivity(
                        label="Verifying the Skill catalog",
                        waiting_label="Still verifying the Skill catalog",
                        guidance="Heartwood is checking signed metadata and immutable digests.",
                    ),
                )
                expected_tree_sha256 = _confirm_skill_installation(
                    parser,
                    summary,
                    automation_command=(
                        "heartwood skills install "
                        f"{shlex.quote(args.name)}"
                        + (
                            f" --source {shlex.quote(args.source_id)}"
                            if args.source_id is not None
                            else ""
                        )
                        + " --approve --expected-tree-sha256 "
                        f"sha256:{summary['tree_sha256']}"
                    ),
                )
                if expected_tree_sha256 is None:
                    return 1
            settings = _run_with_progress(
                lambda: gateway.install_skill(
                    args.name,
                    source_id=args.source_id,
                    expected_tree_sha256=expected_tree_sha256,
                    approved=True,
                ),
                activity=InteractionActivity(
                    label="Installing the verified Skill",
                    waiting_label="Still installing the verified Skill",
                    guidance="Large Skill packages or remote sources can take additional time.",
                ),
            )
            print(_format_skill_settings(settings))
            return 0
        if command == "inspect-local":
            print(_format_skill_summary(gateway.inspect_local_skill(args.source)))
            return 0
        if command == "install-local":
            expected_tree_sha256 = args.expected_tree_sha256
            if args.approve:
                if expected_tree_sha256 is None:
                    parser.error(
                        "--approve requires --expected-tree-sha256 with the digest shown by "
                        "`heartwood skills inspect-local`"
                    )
            else:
                if expected_tree_sha256 is not None:
                    parser.error("--expected-tree-sha256 requires --approve")
                summary = gateway.inspect_local_skill(args.source)
                expected_tree_sha256 = _confirm_skill_installation(
                    parser,
                    summary,
                    automation_command=(
                        "heartwood skills install-local "
                        f"{shlex.quote(str(args.source))} --approve --expected-tree-sha256 "
                        f"sha256:{summary['tree_sha256']}"
                    ),
                )
                if expected_tree_sha256 is None:
                    return 1
            print(
                _format_skill_settings(
                    gateway.install_local_skill(
                        args.source,
                        expected_tree_sha256=expected_tree_sha256,
                        approved=True,
                    )
                )
            )
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
        source = item["source_id"] if item["source"] == "catalog" else item["source"]
        lines.append(
            f"{item['name']}  version={item['version']}  status={item['status']}  source={source}"
        )
        lines.append(f"    {item['description']}")
        if item["compatibility_reason"] is not None:
            lines.append(f"    {item['compatibility_reason']}")
    return "\n".join(lines)


def _format_skill_summary(summary: SkillSummaryResponse) -> str:
    tool_text = ", ".join(summary["declared_tools"])
    review = (
        "Repository reviewed"
        if summary["review"] == "repository-reviewed"
        else "Local and unreviewed"
    )
    controlled_data = "Deployment approved" if summary["controlled_data_ready"] else "Not approved"
    lines = [
        f"Skill: {summary['name']} {summary['version']}",
        f"Review: {review}",
        f"Source: {summary['source_id']}",
        f"Digest: sha256:{summary['tree_sha256']}",
        f"Tools: {tool_text or 'None declared'}",
        f"Network: {'required' if summary['requires_network'] else 'disabled'}",
        f"Data access: {summary['data_access_summary']}",
        f"Dataset types: {', '.join(summary['dataset_types']) or 'Not declared'}",
        f"Controlled data: {controlled_data}",
        f"Permissions: {summary['approval_summary']}",
    ]
    if summary["archive_size"] is not None:
        lines.insert(4, f"Download: {_format_transfer_bytes(summary['archive_size'])}")
    if summary["revocation_reason"] is not None:
        lines.append(f"Revoked: {summary['revocation_reason']}")
    if summary["compatibility_reason"] is not None:
        lines.append(f"Compatibility: {summary['compatibility_reason']}")
    return "\n".join(lines)


def _confirm_skill_installation(
    parser: argparse.ArgumentParser,
    summary: SkillSummaryResponse,
    *,
    automation_command: str,
) -> str | None:
    """Show one exact revision and obtain interactive or digest-bound approval."""
    print(_format_skill_summary(summary))
    if not sys.stdin.isatty():
        parser.error(
            "interactive approval is unavailable; rerun with the exact reviewed digest:\n"
            f"  {automation_command}"
        )
    approved = input("Install this exact Skill revision? [y/N]: ").strip().lower() == "y"
    if not approved:
        print("Skill installation canceled.")
        return None
    return summary["tree_sha256"]


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


def _wait_for_model_transfer(
    gateway: SessionGateway,
    initial: ModelTransferResponse,
) -> ModelTransferResponse:
    """Wait for one shared transfer while rendering byte progress and cancellation."""
    transfer_id = initial["transfer_id"]
    current = initial
    animated = sys.stderr.isatty() and "NO_COLOR" not in os.environ
    started = time.monotonic()
    last_report = started
    cancelled = False
    if not animated:
        print(
            f"{current['phase'].capitalize()} model transfer "
            f"({_format_transfer_bytes(current['bytes_total'])}). "
            "Large models can take several minutes; progress will update here.",
            file=sys.stderr,
            flush=True,
        )
    try:
        while current["status"] in {"running", "cancelling"}:
            total = max(current["bytes_total"], 1)
            processed = min(current["bytes_processed"], total)
            percentage = int((processed / total) * 100)
            phase = current["phase"].replace("-", " ").capitalize()
            elapsed = int(time.monotonic() - started)
            if animated:
                print(
                    f"\r\033[2K{phase} model... {percentage}% "
                    f"({_format_transfer_bytes(processed)} of "
                    f"{_format_transfer_bytes(total)}, {elapsed}s elapsed)",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
            elif time.monotonic() - last_report >= 15:
                print(
                    f"{phase} model: {percentage}% "
                    f"({_format_transfer_bytes(processed)} of {_format_transfer_bytes(total)}, "
                    f"{elapsed}s elapsed).",
                    file=sys.stderr,
                    flush=True,
                )
                last_report = time.monotonic()
            time.sleep(0.2)
            current = gateway.model_transfer_status(transfer_id)
    except KeyboardInterrupt:
        cancelled = True
        current = gateway.cancel_model_transfer(transfer_id)
        if animated:
            print("\r\033[2KStopping model transfer safely...", end="", file=sys.stderr)
        else:
            print("Stopping model transfer safely...", file=sys.stderr)
        while current["status"] in {"running", "cancelling"}:
            time.sleep(0.2)
            current = gateway.model_transfer_status(transfer_id)
    finally:
        if animated:
            print("\r\033[2K", end="", file=sys.stderr, flush=True)
    if current["status"] == "ready":
        return current
    if current["status"] == "cancelled" or cancelled:
        raise ModelTransferError("model transfer was cancelled")
    raise ModelTransferError(current["error"] or "model transfer failed")


def _format_transfer_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024**2:
        return f"{value / 1024:.1f} KiB"
    if value < 1024**3:
        return f"{value / 1024**2:.1f} MiB"
    return f"{value / 1024**3:.2f} GiB"


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


def _handle_audit_export(
    gateway: SessionGateway,
    *,
    session_id: str,
    output: Path | None,
) -> int:
    events = gateway.handle(_command(session_id=session_id, kind=CommandKind.AUDIT_EXPORT)).events
    export_path = Path(str(events[-1].payload["path"]))
    if output is not None:
        output = gateway.copy_audit_export(session_id, output)
    print(f"Audit export: {output or export_path}")
    return 0


def _handle_audit_verify(gateway: SessionGateway, *, session_id: str) -> int:
    verification = gateway.verify_audit(session_id)
    print(
        "\n".join(
            (
                "Audit history verified",
                f"Events: {verification.event_count}",
                f"Terminal hash: {verification.terminal_event_hash or 'none'}",
                f"Export digest: {verification.content_sha256}",
            )
        )
    )
    return 0


def _handle_audit_signer(
    gateway: SessionGateway,
    *,
    command: str | None,
    args: argparse.Namespace,
) -> int:
    if command == "select":
        profile = gateway.select_checkpoint_signer(args.profile_id)
        print(f"Checkpoint signer selected: {profile.profile_id}")
        return 0
    if command == "default":
        profile = gateway.select_checkpoint_signer(None)
        print(f"Checkpoint signer: {profile.profile_id} (deployment default)")
        return 0
    profiles = gateway.checkpoint_signers()
    if not profiles:
        print("No deployment checkpoint signer registry is installed.")
        print("Production checkpoints remain unavailable until an operator configures one.")
        return 1
    active = gateway.active_checkpoint_signer().profile_id
    default = gateway.default_checkpoint_signer().profile_id
    print("Checkpoint signers")
    for profile in profiles:
        markers = []
        if profile.profile_id == active:
            markers.append("active")
        if profile.profile_id == default:
            markers.append("deployment default")
        suffix = f" ({', '.join(markers)})" if markers else ""
        print(f"  {profile.profile_id}{suffix}: {profile.mode}, {profile.algorithm}")
    return 0


def _handle_audit_checkpoint(
    gateway: SessionGateway,
    *,
    session_id: str,
    output: Path,
    deployment_id: str,
    retention_policy_id: str,
    retain_until: str,
) -> int:
    verification = gateway.create_audit_checkpoint(
        session_id=session_id,
        output=output,
        deployment_id=deployment_id,
        retention_policy_id=retention_policy_id,
        retain_until=retain_until,
    )
    statement = verification.checkpoint.statement
    signature = verification.checkpoint.signature
    print(
        "\n".join(
            (
                f"Audit checkpoint: {output.expanduser().resolve()}",
                f"Events: {statement.audit_event_count}",
                f"Signer: {signature.signer_id}",
                f"Signing key: {signature.key_id} ({signature.key_version})",
                f"Retention: {statement.retention.policy_id} through "
                f"{statement.retention.retain_until}",
            )
        )
    )
    return 0


def _handle_audit_checkpoint_verification(
    gateway: SessionGateway,
    *,
    bundle: Path,
    public_key: Path | None,
) -> int:
    verification = gateway.verify_audit_checkpoint(bundle=bundle, public_key=public_key)
    statement = verification.checkpoint.statement
    print(
        "\n".join(
            (
                "Audit checkpoint verified",
                f"Deployment: {statement.deployment_id}",
                f"Session: {statement.session_id}",
                f"Events: {statement.audit_event_count}",
                f"Retention: {statement.retention.policy_id} through "
                f"{statement.retention.retain_until}",
            )
        )
    )
    return 0


def _handle_local_signer_init(
    *,
    project: ProjectContext,
    directory: Path | None,
    profile_id: str,
    key_version: str,
    port: int,
) -> int:
    root = (
        user_checkpoint_signer_registry_path(os.environ).parent
        if directory is None
        else directory.expanduser()
    )
    _require_deployment_owned_cli_path(project, root, label="local signer directory")
    setup = initialize_local_checkpoint_signer(
        directory=root,
        profile_id=profile_id,
        endpoint=f"http://127.0.0.1:{port}/v1/checkpoints/sign",
        key_version=key_version,
    )
    print(
        "\n".join(
            (
                "Local checkpoint signer initialized",
                f"Registry: {setup.registry}",
                f"Profile: {setup.profile_id}",
                "",
                "Start it with:",
                f"heartwood signer serve-local --registry {setup.registry} "
                f"--profile {setup.profile_id}",
                "",
                f"Then select it in a project with: heartwood audit signer select "
                f"{setup.profile_id}",
            )
        )
    )
    return 0


def _handle_local_signer_serve(
    *,
    project: ProjectContext,
    registry: CheckpointSignerRegistry,
    profile_id: str | None,
    private_key: Path | None,
    host: str | None,
    port: int | None,
) -> int:
    if registry.source is not None:
        _require_deployment_owned_cli_path(
            project,
            registry.source,
            label="checkpoint signer registry",
        )
    profile = registry.profile(profile_id)
    if profile.mode != "development":
        raise CheckpointSignerError("serve-local requires a development signer profile")
    _require_deployment_owned_cli_path(
        project,
        profile.trusted_public_key,
        label="trusted checkpoint public key",
    )
    if profile.authorization_token_file is not None:
        _require_deployment_owned_cli_path(
            project,
            profile.authorization_token_file,
            label="checkpoint signer authorization token",
        )
    configured_endpoint = urlsplit(profile.endpoint)
    configured_host = configured_endpoint.hostname
    configured_port = configured_endpoint.port
    if configured_host is None or configured_port is None:
        raise CheckpointSignerError("local signer registry endpoint must include a host and port")
    selected_host = configured_host if host is None else host
    selected_port = configured_port if port is None else port
    if selected_host not in {"127.0.0.1", "::1"}:
        raise CheckpointSignerError("local signer must bind a loopback host")
    host_url = f"[{selected_host}]" if ":" in selected_host else selected_host
    expected_endpoint = f"http://{host_url}:{selected_port}/v1/checkpoints/sign"
    if configured_endpoint != urlsplit(expected_endpoint):
        raise CheckpointSignerError(
            "local signer host and port do not match the deployment registry"
        )
    if private_key is None:
        if registry.source is None:
            raise CheckpointSignerError(
                "local signer private key must be provided when the registry has no source"
            )
        private_key = registry.source.parent / "local-checkpoint-private.pem"
    resolved_private_key = _require_deployment_owned_cli_path(
        project,
        private_key,
        label="local signer private key",
    )
    signer = LocalEd25519CheckpointSigner(
        private_key=resolved_private_key,
        signer_id=profile.signer_id,
        key_id=profile.key_id,
        key_version=profile.key_version,
    )
    if checkpoint_public_key_fingerprint(signer.public_key) != profile.public_key_sha256:
        raise CheckpointSignerError("local signer private key does not match the registry")
    token = profile.authorization_token()
    if token is None:  # pragma: no cover - registry validation enforces this
        raise CheckpointSignerError("local signer authorization token is unavailable")
    print(f"Local checkpoint signer: {expected_endpoint}")
    print("Keep this process running while creating checkpoints.")
    uvicorn.run(
        LocalCheckpointSignerApp(signer, authorization_token=token),
        host=selected_host,
        port=selected_port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
    )
    return 0


def _require_deployment_owned_cli_path(
    project: ProjectContext,
    path: Path,
    *,
    label: str,
) -> Path:
    expanded = path.expanduser()
    resolved = expanded if expanded.is_absolute() else (Path.cwd() / expanded)
    resolved = resolved.resolve()
    if resolved == project.root or project.root in resolved.parents:
        raise CheckpointSignerError(f"{label} must be outside the Heartwood project")
    return resolved


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
) -> int:
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
    except IngressConfigurationError as error:
        raise SystemExit(f"{diagnostic.code}: {error}") from error
    app = GatewayAsgiApp(
        SessionGateway(project=project),
        static_dir=web_root,
        ingress=ingress,
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        proxy_headers=False,
    )
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
