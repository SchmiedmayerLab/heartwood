# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Command-line presentation of model connections, profiles, and managed model files."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import cast

from heartwood.cli._interactive import InteractionActivity
from heartwood.cli._progress import _format_transfer_bytes, _run_with_progress
from heartwood.gateway import (
    CredentialStoreError,
    ModelArtifactError,
    ModelCatalogError,
    ModelProfile,
    ModelRepositoryError,
    ModelSettingsError,
    ModelSnapshotError,
    ModelTransferError,
    SessionGateway,
    action_mode_label,
)
from heartwood.schemas import (
    ModelArtifactsResponse,
    ModelCatalogResponse,
    ModelRepositoryPlanResponse,
    ModelSettingsResponse,
    ModelTransferPlanResponse,
    ModelTransferResponse,
    ModelValidationResponse,
)

_MODEL_PREPARATION_ACTIVITY = InteractionActivity(
    label="Preparing and verifying the model",
    waiting_label="Still preparing and verifying the model",
    guidance=(
        "Large downloads and full verification of existing model files can take several minutes. "
        "Keep this process running."
    ),
)


def configure_models(models: argparse.ArgumentParser) -> None:
    """Attach model connection, profile, and managed-model commands."""
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


def _format_model_settings(settings: ModelSettingsResponse) -> str:
    lines = [
        "Heartwood models",
        "",
        f"Credential isolation: {settings['credential_isolation']['summary']}",
        f"Data handling: {settings['data_handling_summary']}",
        "",
        "Connections:",
    ]
    for connection in settings["connections"]:
        lines.append(
            f"  {connection['connection_id']}  {connection['label']}  "
            f"source={connection['source']}  credentials={connection['credential_status']}"
        )
        lines.append(f"    data handling: {connection['data_handling_summary']}")
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


_TOOL_SUPPORT = {None: "unknown", True: "supported", False: "not supported"}


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
        window = item["context_window"]
        lines.append(
            "    Context window: "
            + ("unknown" if window is None else f"{window:,} tokens")
            + "; native tool calls: "
            + _TOOL_SUPPORT[item["supports_tools"]]
        )
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
            f"Data handling: {validation['data_handling_summary']}",
            f"Action review: {action_mode_label(validation['action_review_mode'])}",
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
