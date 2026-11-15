# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Command-line guided setup of a model route and conservative policy."""

from __future__ import annotations

import argparse
import getpass
import os
import tempfile
from pathlib import Path
from typing import cast

from heartwood.adapters.platform import select_platform_adapter
from heartwood.cli._interactive import InteractionActivity
from heartwood.cli._models import (
    _MODEL_PREPARATION_ACTIVITY,
    _format_model_repository,
    _model_tier_label,
)
from heartwood.cli._progress import _run_with_progress
from heartwood.gateway import (
    BUILT_IN_MODEL_CONNECTIONS,
    MODEL_SOURCE_OPTIONS,
    ActionSettingsError,
    CredentialStoreError,
    DeploymentReadiness,
    ModelArtifactError,
    ModelCatalogError,
    ModelRepositoryError,
    ModelSettingsError,
    ModelSnapshotError,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    SessionGateway,
    custom_model_connection_requires_token,
    inspect_deployment,
    model_source_options,
)
from heartwood.schemas import ModelArtifactsResponse, StartupPlanResponse

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


def configure_setup(setup: argparse.ArgumentParser) -> None:
    """Attach the guided setup options."""
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


def _format_readiness(readiness: DeploymentReadiness) -> str:
    lines = [
        "Heartwood environment",
        f"Project: {readiness.project_root}",
        f"Heartwood data: {readiness.state_root}",
        f"Platform: {readiness.platform_id}",
        f"Readiness: {readiness.state}",
        "",
    ]
    policy = readiness.policy.safe_dict()
    lines[-1:-1] = [
        "Policy source: "
        + (
            f"operator deployment manifest {policy['manifest']}"
            if policy["source"] == "deployment" and policy["manifest"]
            else "operator deployment policy"
            if policy["source"] == "deployment"
            else "platform adapter"
        ),
        f"Policy: {policy['policy_id']}",
        f"Review modes: {', '.join(cast(list[str], policy['action_review_modes']))}",
        f"Model endpoints: {_listed(policy['model_endpoints'])}",
        f"Capability tiers: {', '.join(cast(list[str], policy['capability_tiers']))}",
        f"Credential backends: {', '.join(cast(list[str], policy['credential_backends']))}",
        "Checkpoint signers: "
        + (
            "deployment registry"
            if policy["checkpoint_signer_profiles"] is None
            else _listed(policy["checkpoint_signer_profiles"])
        ),
        "Data destinations: "
        + ("any" if policy["data_destinations"] is None else _listed(policy["data_destinations"])),
        f"Isolated worker: {policy['isolated_worker']}",
        f"Checkpoint evidence: {policy['checkpoint_evidence']}",
    ]
    markers = {"pass": "OK", "warning": "NOTE", "fail": "FAIL"}
    for check in readiness.checks:
        lines.append(f"[{markers[check.status]}] {check.summary}")
        if check.status != "pass":
            diagnostic = check.safe_dict()
            lines.append(f"       {diagnostic['code']} · {diagnostic['next_action']}")
    return "\n".join(lines)


def _listed(values: object) -> str:
    items = cast(list[str], values)
    return ", ".join(items) if items else "none"


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
    source: str | None = getattr(args, "model_source", None)
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
        selected_option = next(
            option for option in MODEL_SOURCE_OPTIONS if option.source_id == source
        )
        for listed in gateway.model_settings()["connections"]:
            if listed["connection_id"] == selected_option.connection_id:
                print(f"  Data handling: {listed['data_handling_summary']}")
        if source == "heartwood":
            if not resume_managed_local:
                _configure_local_model(
                    gateway,
                    model_id=model_id,
                    non_interactive=non_interactive,
                    yes_download=bool(getattr(args, "yes_download", False)),
                )
            return 0, gateway
        connection_id = selected_option.connection_id
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
    catalog_models = local_catalog["models"]
    recommendations = [item for item in catalog_models if item["available"]]
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

    known_local_ids = {item["model_id"]: item for item in catalog_models}
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
