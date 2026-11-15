# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Heartwood-managed model choices, downloads, imports, transfers, and selection."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path

from heartwood.gateway._gpu_environment import (
    GpuEnvironment,
    inspect_gpu_environment,
    minimum_compute_capability_for_model,
)
from heartwood.gateway._local_import import import_local_model
from heartwood.gateway._local_model_contract import (
    MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW,
    managed_model_token_budgets,
)
from heartwood.gateway._local_models import (
    HuggingFaceModelRepository,
    LocalModelChoice,
    LocalModelDownloadPlan,
    ModelRepositoryError,
    catalog_model_choices,
)
from heartwood.gateway._model_artifacts import (
    LocalModelDownloadManager,
    ModelArtifact,
    ModelArtifactCatalog,
    ModelArtifactError,
    ModelDownload,
    verify_model_artifact,
)
from heartwood.gateway._model_artifacts import (
    download_model_artifact as download_artifact,
)
from heartwood.gateway._model_settings import model_profile_from_preset
from heartwood.gateway._model_snapshots import (
    ModelSnapshot,
    ModelSnapshotCatalog,
    ModelSnapshotError,
    ModelTier,
    automatic_model_tier,
    download_model_snapshot,
)
from heartwood.gateway._model_transfer import (
    ModelTransferError,
    ModelTransferManager,
    inspect_model_bundle,
    model_transfer_identity,
)
from heartwood.gateway._project import ProjectContext
from heartwood.gateway._project_config import LocalModelSelection, ProjectConfigStore
from heartwood.gateway._readiness import gpu_visible, managed_local_runtime_active
from heartwood.schemas import (
    LocalModelImportResponse,
    ModelArtifactsResponse,
    ModelDownloadResponse,
    ModelRepositoryPlanResponse,
    ModelTransferPlanResponse,
    ModelTransferResponse,
    api_response,
)


class LocalModelLifecycle:
    """Own the managed-model catalog view, storage operations, and project selection."""

    def __init__(
        self,
        *,
        project: ProjectContext,
        env: Mapping[str, str],
        config_store: ProjectConfigStore,
        artifact_catalog: ModelArtifactCatalog,
        snapshot_catalog: ModelSnapshotCatalog,
        model_repository: HuggingFaceModelRepository,
        state_lock: AbstractContextManager[object],
        on_selection_changed: Callable[[], None],
    ) -> None:
        self.project = project
        self.env = env
        self.config_store = config_store
        self.artifact_catalog = artifact_catalog
        self.snapshot_catalog = snapshot_catalog
        self.model_repository = model_repository
        self._state_lock = state_lock
        self._on_selection_changed = on_selection_changed
        self._gpu_environment: GpuEnvironment | None = None
        self._verified_local_artifacts: set[tuple[Path, int, int, str]] = set()
        downloadable_choices = catalog_model_choices(
            self.artifact_catalog.artifacts,
            self.snapshot_catalog.snapshots,
            recommended_only=False,
        )
        self._downloadable_local_model_choices = {
            choice.model_id: choice for choice in downloadable_choices
        }
        self._local_model_choices = dict(self._downloadable_local_model_choices)
        self._recommended_local_model_ids = {
            artifact.artifact_id
            for artifact in self.artifact_catalog.artifacts
            if artifact.recommended
        } | {
            snapshot.snapshot_id
            for snapshot in self.snapshot_catalog.snapshots
            if snapshot.recommended
        }
        selected_local_model = self.config_store.load().local_model
        if selected_local_model is not None:
            selected_choice = self._downloadable_local_model_choices.get(
                selected_local_model.artifact_id
            )
            if selected_local_model.catalog_source == "transferred" or (
                selected_choice is None and selected_local_model.catalog_source == "user-selected"
            ):
                selected_choice = selected_local_model.to_choice()
                if selected_local_model.catalog_source == "user-selected":
                    self._downloadable_local_model_choices[selected_choice.model_id] = (
                        selected_choice
                    )
            if selected_choice is not None:
                self._local_model_choices[selected_choice.model_id] = selected_choice
        self._repository_plans: dict[tuple[str, str], LocalModelChoice] = {}
        self.model_cache_dir = self.project.models_dir
        self.local_model_manager = LocalModelDownloadManager(
            artifact_catalog=self.artifact_catalog,
            snapshot_catalog=self.snapshot_catalog,
            cache_dir=self.model_cache_dir,
            on_ready=self.select_downloaded_local_model,
        )
        self.model_transfer_manager = ModelTransferManager(
            models_dir=self.model_cache_dir,
            on_import_ready=self.select_transferred_local_model,
        )

    def model_artifacts(self) -> ModelArtifactsResponse:
        """Return normalized local choices, source metadata, and download status."""
        snapshot_catalog = self.snapshot_catalog.safe_dict()
        statuses = {status.model_id: status for status in self.local_model_manager.statuses()}
        selected = self.config_store.load().local_model
        active_model_id = (
            selected.artifact_id
            if selected is not None and managed_local_runtime_active(selected, self.env)
            else None
        )
        if selected is not None and selected.artifact_id not in statuses:
            path = selected.resolved_path(self.project)
            if path.exists():
                size = selected.size_bytes or self._local_model_size(selected.artifact_id, path)
                try:
                    self._verify_selected_local_artifact(selected, path)
                except (OSError, ValueError) as error:
                    statuses[selected.artifact_id] = ModelDownload(
                        model_id=selected.artifact_id,
                        status="error",
                        bytes_downloaded=0,
                        bytes_total=size,
                        error=f"Selected model integrity check failed: {error}",
                    )
                else:
                    statuses[selected.artifact_id] = ModelDownload(
                        model_id=selected.artifact_id,
                        status="ready",
                        bytes_downloaded=size,
                        bytes_total=size,
                        path=str(path),
                    )
        gpu_environment = self.gpu_environment()
        preferred_runtime = self._preferred_local_runtime()
        local_choices = list(self._local_model_choices.values())
        local_choices.sort(
            key=lambda choice: (
                selected is None or choice.model_id != selected.artifact_id,
                not self.local_runtime_available(choice.runtime),
                choice.runtime != preferred_runtime,
            )
        )
        recommendation = self.recommend_managed_model(
            maximum_tier=automatic_model_tier(gpu_environment.platform_id),
            gpu_environment=gpu_environment,
        )
        preferred_id = (
            recommendation.snapshot_id
            if recommendation is not None
            else next(
                (
                    choice.model_id
                    for choice in local_choices
                    if choice.model_id in self._recommended_local_model_ids
                    and choice.minimum_gpu_count == 0
                    and choice.qualification_for(gpu_environment.platform_id) == "qualified"
                    and self.local_runtime_available(choice.runtime)
                    and choice.runtime == preferred_runtime
                ),
                None,
            )
        )
        choices = [
            self._local_model_choice_dict(
                choice,
                active=choice.model_id == active_model_id,
                selected=selected is not None and choice.model_id == selected.artifact_id,
                recommendation=(
                    "Selected for this project"
                    if selected is not None and choice.model_id == selected.artifact_id
                    else (
                        "Recommended for this deployment"
                        if selected is None and choice.model_id == preferred_id
                        else None
                    )
                ),
                gpu_environment=gpu_environment,
            )
            for choice in local_choices
        ]
        return api_response(
            ModelArtifactsResponse,
            {
                **self.artifact_catalog.safe_dict(),
                "snapshot_schema_version": snapshot_catalog["schema_version"],
                "snapshots": snapshot_catalog["snapshots"],
                "models": choices,
                "downloads": [status.safe_dict() for status in statuses.values()],
                "transfers": [
                    transfer.safe_dict() for transfer in self.model_transfer_manager.statuses()
                ],
                "gpu_environment": {
                    "platform_id": gpu_environment.platform_id,
                    "capacities": [
                        {
                            "label": capacity.label,
                            "gpu_model": capacity.gpu_model,
                            "gpu_count": capacity.gpu_count,
                            "gpu_memory_bytes": capacity.gpu_memory_bytes,
                            "allocation_required": capacity.allocation_required,
                            "partition": capacity.partition,
                        }
                        for capacity in gpu_environment.capacities
                    ],
                },
            },
        )

    def gpu_environment(self, *, refresh: bool = False) -> GpuEnvironment:
        """Return the shared GPU and scheduler inventory for this deployment."""
        if refresh or self._gpu_environment is None:
            self._gpu_environment = inspect_gpu_environment(
                self.config_store.load().platform_id,
                self.env,
            )
        return self._gpu_environment

    def recommend_managed_model(
        self,
        *,
        maximum_tier: ModelTier,
        requested_gpus: int | None = None,
        gpu_environment: GpuEnvironment | None = None,
    ) -> ModelSnapshot | None:
        """Choose one qualified catalog model for the detected resource envelopes."""
        environment = gpu_environment or self.gpu_environment()
        return self.snapshot_catalog.recommend_for_capacities(
            platform_id=environment.platform_id,
            capacities=tuple(
                (capacity.gpu_count, capacity.gpu_memory_bytes)
                for capacity in environment.capacities
            ),
            maximum_tier=maximum_tier,
            requested_gpus=requested_gpus,
        )

    def inspect_model_repository(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelRepositoryPlanResponse:
        """Build one automatic download plan without downloading model weights."""
        plan = self._repository_plan(repository, revision=revision)
        self._repository_plans[(repository.strip(), (revision or "").strip())] = plan.model
        self._repository_plans[(plan.model.source_repository, plan.model.source_revision)] = (
            plan.model
        )
        return api_response(
            ModelRepositoryPlanResponse,
            {
                "model": self._local_model_choice_dict(plan.model),
                "selection_reason": plan.selection_reason,
            },
        )

    def download_local_model(self, model_id: str) -> ModelDownloadResponse:
        """Start a known local-model download into project storage."""
        choice = self._require_downloadable_local_model(model_id)
        return api_response(
            ModelDownloadResponse,
            self.local_model_manager.start_model(choice.download_model()).safe_dict(),
        )

    def download_custom_local_model(
        self,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelDownloadResponse:
        """Resolve and start one user-selected Hugging Face model download."""
        choice = self._custom_local_model_choice(repository, revision=revision)
        return api_response(
            ModelDownloadResponse,
            self.local_model_manager.start_model(choice.download_model()).safe_dict(),
        )

    def download_local_model_now(
        self,
        model_id: str,
        *,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download and verify a known model, selecting it when agent-compatible."""
        return self._download_and_select(
            self._require_downloadable_local_model(model_id),
            progress_callback=progress_callback,
        )

    def download_custom_local_model_now(
        self,
        repository: str,
        *,
        revision: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Resolve, download, verify, and select one user-selected model."""
        return self._download_and_select(
            self._custom_local_model_choice(repository, revision=revision),
            progress_callback=progress_callback,
        )

    def import_local_model(
        self,
        source: Path,
        *,
        source_repository: str,
        source_revision: str,
        license_posture: str,
        context_window: int,
    ) -> LocalModelImportResponse:
        """Import and select a reviewed local GGUF file or vLLM snapshot."""
        imported = import_local_model(
            source,
            models_dir=self.model_cache_dir,
            source_repository=source_repository,
            source_revision=source_revision,
            license_posture=license_posture,
            context_window=context_window,
        )
        choice = imported.model
        previous_config = self.config_store.load() if self.config_store.configured else None
        self._local_model_choices[choice.model_id] = choice
        self._downloadable_local_model_choices[choice.model_id] = choice
        runtime_profile = "llama-cpp-cpu" if choice.runtime == "llama-cpp" else "vllm-cuda"
        try:
            self.select_downloaded_local_model(choice.model_id, imported.path, runtime_profile)
        except Exception:
            self._local_model_choices.pop(choice.model_id, None)
            self._downloadable_local_model_choices.pop(choice.model_id, None)
            self.config_store.restore(previous_config)
            shutil.rmtree(imported.storage_root, ignore_errors=True)
            raise
        selected = self.config_store.load().local_model
        selected_for_project = selected is not None and selected.artifact_id == choice.model_id
        return api_response(
            LocalModelImportResponse,
            {
                "model": self._local_model_choice_dict(
                    choice,
                    active=(
                        selected_for_project
                        and selected is not None
                        and managed_local_runtime_active(selected, self.env)
                    ),
                    selected=selected_for_project,
                    recommendation="Selected for this project",
                ),
                "path": str(imported.path),
                "status": "ready",
            },
        )

    def inspect_local_model_bundle(self, path: Path) -> ModelTransferPlanResponse:
        """Inspect one portable model bundle without changing project state."""
        plan = inspect_model_bundle(path)
        choice = self._trusted_transferred_model_choice(plan.model)
        warnings = self._model_transfer_warnings(choice)
        return api_response(
            ModelTransferPlanResponse,
            {
                "bundle_path": str(plan.bundle_path),
                "bundle_size_bytes": plan.bundle_size_bytes,
                "manifest_sha256": plan.manifest_sha256,
                "file_count": len(plan.manifest.files),
                "runtime_profile": plan.manifest.runtime_profile,
                "model": self._local_model_choice_dict(choice),
                "warnings": list(warnings),
            },
        )

    def export_local_model(self, path: Path) -> ModelTransferResponse:
        """Start a verified export of the selected Heartwood-managed model."""
        selected = self.config_store.load().local_model
        if selected is None:
            raise ModelTransferError(
                "select or download a Heartwood-managed model before exporting a bundle"
            )
        choice = self._local_model_choices.get(selected.artifact_id)
        if choice is None:
            choice = selected.to_choice()
        transfer = self.model_transfer_manager.start_export(
            choice=choice,
            model_path=selected.resolved_path(self.project),
            bundle_path=path,
            warnings=self._model_transfer_warnings(choice),
        )
        return api_response(ModelTransferResponse, transfer.safe_dict())

    def import_local_model_bundle(
        self,
        path: Path,
        *,
        approved: bool,
        manifest_sha256: str,
    ) -> ModelTransferResponse:
        """Start an atomic model-bundle import after explicit review."""
        plan = inspect_model_bundle(path)
        if plan.manifest_sha256 != manifest_sha256:
            raise ModelTransferError(
                "model bundle manifest changed after review; inspect the bundle again"
            )
        choice = self._trusted_transferred_model_choice(plan.model)
        transfer = self.model_transfer_manager.start_import(
            plan=plan,
            approved=approved,
            warnings=self._model_transfer_warnings(choice),
        )
        return api_response(ModelTransferResponse, transfer.safe_dict())

    def cancel_model_transfer(self, transfer_id: str) -> ModelTransferResponse:
        """Request cancellation of one active model transfer."""
        return api_response(
            ModelTransferResponse,
            self.model_transfer_manager.cancel(transfer_id).safe_dict(),
        )

    def model_transfer_status(self, transfer_id: str) -> ModelTransferResponse:
        """Return one shared model-transfer status snapshot."""
        return api_response(
            ModelTransferResponse,
            self.model_transfer_manager.status(transfer_id).safe_dict(),
        )

    def select_downloaded_local_model(
        self,
        model_id: str,
        path: Path,
        runtime_profile: str,
    ) -> None:
        """Select one downloaded or imported model that this gateway already knows."""
        with self._state_lock:
            choice = self._downloadable_local_model_choices.get(model_id)
            if choice is None:
                raise ModelRepositoryError(
                    f"Heartwood-managed model metadata is unavailable: {model_id}"
                )
            self._select_local_model(choice, path, runtime_profile)

    def select_transferred_local_model(
        self,
        choice: LocalModelChoice,
        path: Path,
        runtime_profile: str,
    ) -> None:
        """Register and select one bundle model through the normal managed-model path."""
        with self._state_lock:
            choice = self._trusted_transferred_model_choice(choice)
            previous_choice = self._local_model_choices.get(choice.model_id)
            previous_config = self.config_store.load() if self.config_store.configured else None
            self._local_model_choices[choice.model_id] = choice
            try:
                self._select_local_model(choice, path, runtime_profile)
            except Exception:
                if previous_choice is None:
                    self._local_model_choices.pop(choice.model_id, None)
                else:
                    self._local_model_choices[choice.model_id] = previous_choice
                self.config_store.restore(previous_config)
                raise

    def local_runtime_available(self, runtime: str) -> bool:
        """Return whether this deployment can launch one managed runtime now."""
        platform_id = self.config_store.load().platform_id
        installed = self.local_runtime_installed(runtime)
        if runtime == "llama-cpp":
            return installed
        if runtime != "vllm":
            return False
        if platform_id == "carina":
            return installed
        return installed and gpu_visible(self.env)

    def local_runtime_installed(self, runtime: str) -> bool:
        """Return whether this deployment carries one managed runtime executable."""
        executable_path = self.env.get("PATH")
        if runtime == "llama-cpp":
            return _runtime_executable_available(Path("/opt/llama.cpp/llama-server")) or (
                executable_path is not None
                and shutil.which("llama-server", path=executable_path) is not None
            )
        if runtime != "vllm":
            return False
        if self.config_store.load().platform_id == "carina":
            return True
        return _runtime_executable_available(Path("/opt/heartwood-vllm/bin/vllm")) or (
            executable_path is not None and shutil.which("vllm", path=executable_path) is not None
        )

    def _download_and_select(
        self,
        choice: LocalModelChoice,
        *,
        progress_callback: Callable[[int, int], None] | None,
    ) -> Path:
        model = choice.download_model()
        if isinstance(model, ModelArtifact):
            path = download_artifact(
                model,
                cache_dir=self.model_cache_dir,
                progress_callback=progress_callback,
            )
        else:
            path = download_model_snapshot(
                model,
                cache_dir=self.model_cache_dir,
                progress_callback=progress_callback,
            )
        self.select_downloaded_local_model(choice.model_id, path, model.runtime_profile)
        return path

    def _verify_selected_local_artifact(
        self,
        selected: LocalModelSelection,
        path: Path,
    ) -> None:
        runtime = selected.runtime
        if runtime == "auto":
            runtime = "llama-cpp" if path.suffix.casefold() == ".gguf" else "vllm"
        if runtime != "llama-cpp":
            return
        if selected.size_bytes is None or selected.artifact_sha256 is None:
            raise ModelArtifactError(
                "selected llama.cpp artifact is missing persisted size or checksum metadata"
            )
        stat = path.stat()
        cache_key = (path, stat.st_size, stat.st_mtime_ns, selected.artifact_sha256)
        if cache_key in self._verified_local_artifacts:
            return
        verify_model_artifact(
            path,
            expected_size_bytes=selected.size_bytes,
            expected_sha256=selected.artifact_sha256,
        )
        self._verified_local_artifacts = {cache_key}

    def _select_local_model(
        self,
        choice: LocalModelChoice,
        path: Path,
        runtime_profile: str,
    ) -> None:
        """Select one verified local model through the shared project contract."""
        if choice.context_window < MINIMUM_AGENT_RUNTIME_CONTEXT_WINDOW:
            return
        input_capacity, output_budget = managed_model_token_budgets(choice.context_window)
        config = self.config_store.load()
        selection = LocalModelSelection.from_choice(
            choice,
            path=path,
            runtime=_runtime_kind(runtime_profile),
            platform_id=config.platform_id,
        )
        profile = replace(
            model_profile_from_preset("heartwood-managed", selection.model_id),
            profile_id="heartwood",
            description=choice.label,
            max_input_tokens=input_capacity,
            max_output_tokens=output_budget,
        )
        self.config_store.select_local_model(
            selection,
            settings=config.model_settings.with_profile(profile).selecting(profile.profile_id),
        )
        self._on_selection_changed()

    def _trusted_transferred_model_choice(self, choice: LocalModelChoice) -> LocalModelChoice:
        """Reject identity collisions and never trust bundle-supplied qualification."""
        choice.validate()
        try:
            managed_model_token_budgets(choice.context_window)
        except ValueError as error:
            raise ModelRepositoryError(str(error)) from error
        existing = self._local_model_choices.get(choice.model_id)
        if existing is not None and model_transfer_identity(choice) != model_transfer_identity(
            existing
        ):
            raise ModelTransferError(
                f"model bundle metadata conflicts with the configured catalog: {choice.model_id}"
            )
        trusted = self._downloadable_local_model_choices.get(choice.model_id)
        if trusted is not None and model_transfer_identity(choice) == model_transfer_identity(
            trusted
        ):
            return replace(trusted, catalog_source="transferred")
        return replace(
            choice,
            qualification="unvalidated",
            validated_platforms=(),
            qualification_test=None,
            qualification_date=None,
            qualification_evidence=None,
        )

    def _model_transfer_warnings(self, choice: LocalModelChoice) -> tuple[str, ...]:
        """Return shared import/export guidance for one transfer model."""
        warnings = [
            "Verify the upstream license and use an institution-approved transfer channel; "
            "bundle checksums provide integrity, not transfer authorization."
        ]
        platform_id = self.config_store.load().platform_id
        if choice.qualification_for(platform_id) != "qualified":
            warnings.append(
                "This exact model configuration has not completed Heartwood qualification "
                f"for {platform_id}."
            )
        if not self.local_runtime_installed(choice.runtime):
            runtime = "NVIDIA vLLM" if choice.runtime == "vllm" else "portable CPU"
            warnings.append(
                f"The {runtime} runtime is not installed in this deployment; import is allowed, "
                "but launch will remain unavailable until a compatible runtime is provided."
            )
        return tuple(warnings)

    def _repository_plan(self, repository: str, *, revision: str | None) -> LocalModelDownloadPlan:
        return self.model_repository.plan(
            repository,
            revision=revision,
            cpu_available=self.local_runtime_available("llama-cpp"),
            gpu_available=self.local_runtime_available("vllm"),
        )

    def _custom_local_model_choice(
        self,
        repository: str,
        *,
        revision: str | None,
    ) -> LocalModelChoice:
        key = (repository.strip(), (revision or "").strip())
        choice = self._repository_plans.get(key)
        if choice is None:
            choice = self._repository_plan(repository, revision=revision).model
        existing = self._local_model_choices.get(choice.model_id)
        if existing is not None and existing != choice:
            raise ModelRepositoryError(f"Heartwood-managed model id collision: {choice.model_id}")
        self._local_model_choices[choice.model_id] = choice
        self._downloadable_local_model_choices[choice.model_id] = choice
        return choice

    def _local_model_choice_dict(
        self,
        choice: LocalModelChoice,
        *,
        active: bool = False,
        selected: bool = False,
        recommendation: str | None = None,
        gpu_environment: GpuEnvironment | None = None,
    ) -> dict[str, object]:
        platform_id = (
            gpu_environment.platform_id
            if gpu_environment is not None
            else self.config_store.load().platform_id
        )
        qualification = choice.qualification_for(platform_id)
        runtime_available = self.local_runtime_available(choice.runtime)
        resource_reason: str | None = None
        available = runtime_available
        if choice.runtime == "vllm" and runtime_available:
            environment = gpu_environment or self.gpu_environment()
            available, resource_reason = environment.assess(
                gpu_count=choice.tensor_parallel_size,
                gpu_memory_bytes=choice.minimum_gpu_memory_bytes,
                minimum_compute_capability=minimum_compute_capability_for_model(
                    model_id=choice.source_repository,
                    precision=choice.precision,
                ),
            )
        if qualification != "qualified":
            candidate_reason = "No completed Heartwood qualification exists for this platform"
            recommendation = (
                f"{recommendation}; {candidate_reason.lower()}"
                if recommendation
                else candidate_reason
            )
        unavailable_reason = resource_reason if resource_reason and not available else None
        if resource_reason and available:
            recommendation = (
                f"{recommendation}; {resource_reason}" if recommendation else resource_reason
            )
        reason = _local_model_availability_reason(
            choice.runtime,
            available=available,
            recommendation=recommendation,
            unavailable_reason=unavailable_reason,
        )
        return {
            **choice.safe_dict(),
            "qualification": qualification,
            "qualification_test": (
                choice.qualification_test if qualification == "qualified" else None
            ),
            "qualification_date": (
                choice.qualification_date if qualification == "qualified" else None
            ),
            "qualification_evidence": (
                choice.qualification_evidence if qualification == "qualified" else None
            ),
            "active": active,
            "available": available,
            "selected": selected,
            "availability_reason": reason,
            "recommended": (
                qualification == "qualified"
                and choice.model_id in self._recommended_local_model_ids
                and available
            ),
        }

    def _preferred_local_runtime(self) -> str | None:
        if self.local_runtime_available("vllm"):
            return "vllm"
        if self.local_runtime_available("llama-cpp"):
            return "llama-cpp"
        return None

    def _require_downloadable_local_model(self, model_id: str) -> LocalModelChoice:
        choice = self._downloadable_local_model_choices.get(model_id)
        if choice is None:
            raise ModelRepositoryError(f"unknown Heartwood-managed model: {model_id}")
        if not self.local_runtime_installed(choice.runtime):
            reason = _local_model_availability_reason(
                choice.runtime,
                available=False,
                recommendation=None,
            )
            raise ModelRepositoryError(f"{choice.label} cannot be downloaded: {reason}")
        return choice

    def _local_model_size(self, model_id: str, path: Path) -> int:
        try:
            return self.artifact_catalog.artifact(model_id).artifact_size_bytes
        except ModelArtifactError:
            try:
                return self.snapshot_catalog.snapshot(model_id).expected_size_bytes
            except ModelSnapshotError:
                if path.is_file():
                    return path.stat().st_size
                return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _local_model_availability_reason(
    runtime: str,
    *,
    available: bool,
    recommendation: str | None,
    unavailable_reason: str | None = None,
) -> str:
    if available:
        return recommendation or "Available on this deployment"
    unavailable = unavailable_reason or (
        "Requires a Heartwood NVIDIA GPU runtime"
        if runtime == "vllm"
        else "The portable CPU runtime is not available on this deployment"
    )
    return f"{recommendation}; {unavailable.lower()}" if recommendation else unavailable


def _runtime_executable_available(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _runtime_kind(profile: str) -> str:
    if profile.startswith("llama-cpp"):
        return "llama-cpp"
    if profile.startswith("vllm"):
        return "vllm"
    raise ModelArtifactError(f"unsupported managed runtime profile: {profile}")
