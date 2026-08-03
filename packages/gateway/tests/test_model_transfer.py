# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import cast
from zipfile import ZIP_STORED, ZipFile

import pytest

import heartwood.gateway._model_transfer as model_transfer_module
from heartwood.gateway import (
    LocalModelChoice,
    ModelRepositoryError,
    ModelTransfer,
    ModelTransferError,
    ModelTransferManager,
    ProjectContext,
    RestGateway,
    RestRequest,
    SessionGateway,
    export_model_bundle,
    import_model_bundle,
    inspect_model_bundle,
)
from heartwood.schemas import ModelTransferResponse
from heartwood.session import JsonValue


def test_gguf_bundle_is_reproducible_and_imports_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    choice = _gguf_choice(source)
    first = tmp_path / "first.heartwood-model.zip"
    second = tmp_path / "second.heartwood-model.zip"

    export_model_bundle(choice, model_path=source, bundle_path=first)
    export_model_bundle(choice, model_path=source, bundle_path=second)

    assert first.read_bytes() == second.read_bytes()
    plan = inspect_model_bundle(first)
    assert plan.model.catalog_source == "transferred"
    assert plan.manifest.model.source_revision == "a" * 40
    assert plan.manifest.files[0].sha256 == choice.artifact_sha256

    models = tmp_path / "project" / ".heartwood" / "models"
    imported, selected, profile, created = import_model_bundle(plan, models_dir=models)

    assert created is True
    assert imported.catalog_source == "transferred"
    assert selected.read_bytes() == source.read_bytes()
    assert selected.stat().st_mode & 0o777 == 0o600
    assert profile == "llama-cpp-cpu"
    assert not (models / f".{choice.model_id}.import-partial").exists()


def test_export_never_replaces_an_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    destination = tmp_path / "existing.zip"
    destination.write_bytes(b"existing-transfer-record")

    with pytest.raises(ModelTransferError, match="output already exists"):
        export_model_bundle(
            _gguf_choice(source),
            model_path=source,
            bundle_path=destination,
        )

    assert destination.read_bytes() == b"existing-transfer-record"


def test_export_does_not_clobber_a_destination_created_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    destination = tmp_path / "raced.zip"
    real_link = os.link

    def create_destination_then_link(
        temporary: Path,
        output: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        Path(output).write_bytes(b"concurrent-transfer-record")
        real_link(temporary, output, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", create_destination_then_link)

    with pytest.raises(ModelTransferError, match="output already exists"):
        export_model_bundle(
            _gguf_choice(source),
            model_path=source,
            bundle_path=destination,
        )

    assert destination.read_bytes() == b"concurrent-transfer-record"
    assert not (tmp_path / ".raced.zip.heartwood-partial").exists()


def test_export_recovers_from_private_partial_file_left_by_an_interruption(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    destination = tmp_path / "recovered.zip"
    partial = tmp_path / ".recovered.zip.heartwood-partial"
    partial.write_bytes(b"incomplete")

    export_model_bundle(
        _gguf_choice(source),
        model_path=source,
        bundle_path=destination,
    )

    assert destination.is_file()
    assert not partial.exists()


def test_vllm_bundle_reuses_snapshot_integrity_contract(tmp_path: Path) -> None:
    snapshot = _vllm_snapshot(tmp_path / "snapshot")
    choice = _vllm_choice(snapshot)
    bundle = tmp_path / "model.heartwood-model.zip"

    export_model_bundle(choice, model_path=snapshot, bundle_path=bundle)
    plan = inspect_model_bundle(bundle)
    imported, selected, profile, created = import_model_bundle(
        plan,
        models_dir=tmp_path / "models",
    )

    assert created is True
    assert imported.runtime == "vllm"
    assert selected.is_dir()
    assert (selected / "config.json").is_file()
    assert (selected / "model.safetensors").is_file()
    assert profile == "vllm-cuda"


def test_import_rejects_tampered_incomplete_and_incompatible_bundles(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    bundle = tmp_path / "model.heartwood-model.zip"
    export_model_bundle(_gguf_choice(source), model_path=source, bundle_path=bundle)

    tampered = tmp_path / "tampered.zip"
    _rewrite_bundle(
        bundle,
        tampered,
        replacements={"model/model.gguf": b"GGUFsynthetic-transfer-modeX"},
    )
    with pytest.raises(ModelTransferError, match="checksum mismatch"):
        import_model_bundle(inspect_model_bundle(tampered), models_dir=tmp_path / "tampered")

    incomplete = tmp_path / "incomplete.zip"
    _rewrite_bundle(bundle, incomplete, dropped={"model/model.gguf"})
    with pytest.raises(ModelTransferError, match="contents do not match"):
        inspect_model_bundle(incomplete)

    incompatible = tmp_path / "incompatible.zip"
    manifest = _manifest(bundle)
    manifest["runtime_profile"] = "vllm-cuda"
    _rewrite_bundle(
        bundle,
        incompatible,
        replacements={
            "heartwood-model-bundle.json": _canonical_json(manifest),
        },
    )
    with pytest.raises(ModelTransferError, match="runtime profile"):
        inspect_model_bundle(incompatible)


def test_import_is_idempotent_but_rejects_a_modified_existing_model(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    choice = _gguf_choice(source)
    bundle = tmp_path / "model.heartwood-model.zip"
    export_model_bundle(choice, model_path=source, bundle_path=bundle)
    plan = inspect_model_bundle(bundle)
    models = tmp_path / "models"

    first = import_model_bundle(plan, models_dir=models)
    second = import_model_bundle(plan, models_dir=models)

    assert first[1] == second[1]
    assert first[3] is True
    assert second[3] is False

    second[1].write_bytes(b"GGUFmodified-existing-model")
    with pytest.raises(ModelTransferError, match="checksum mismatch"):
        import_model_bundle(plan, models_dir=models)


def test_import_recovers_from_private_staging_left_by_an_interruption(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    choice = _gguf_choice(source)
    bundle = tmp_path / "model.heartwood-model.zip"
    export_model_bundle(choice, model_path=source, bundle_path=bundle)
    models = tmp_path / "models"
    staging = models / f".{choice.model_id}.import-partial"
    staging.mkdir(parents=True)
    (staging / "incomplete-model.gguf").write_bytes(b"incomplete")

    imported, selected, _profile, created = import_model_bundle(
        inspect_model_bundle(bundle),
        models_dir=models,
    )

    assert imported.model_id == choice.model_id
    assert created is True
    assert selected.read_bytes() == source.read_bytes()
    assert not staging.exists()


def test_cancelled_transfers_remove_partial_outputs(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    choice = _gguf_choice(source)
    bundle = tmp_path / "model.heartwood-model.zip"
    cancelled = Event()
    cancelled.set()

    with pytest.raises(ModelTransferError, match="cancelled"):
        export_model_bundle(
            choice,
            model_path=source,
            bundle_path=bundle,
            cancel=cancelled,
        )
    assert not bundle.exists()
    assert not (tmp_path / ".model.heartwood-model.zip.heartwood-partial").exists()

    cancelled.clear()
    export_model_bundle(choice, model_path=source, bundle_path=bundle)
    cancelled.set()
    models = tmp_path / "models"
    with pytest.raises(ModelTransferError, match="cancelled"):
        import_model_bundle(inspect_model_bundle(bundle), models_dir=models, cancel=cancelled)
    assert not (models / choice.model_id).exists()
    assert not (models / f".{choice.model_id}.import-partial").exists()

    cancelled.clear()
    imported = import_model_bundle(
        inspect_model_bundle(bundle),
        models_dir=models,
        cancel=cancelled,
    )
    assert imported[3] is True
    assert imported[1].read_bytes() == source.read_bytes()


def test_import_rejects_insufficient_destination_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    bundle = tmp_path / "model.heartwood-model.zip"
    export_model_bundle(_gguf_choice(source), model_path=source, bundle_path=bundle)
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: type("DiskUsage", (), {"free": 0})(),
    )

    with pytest.raises(ModelTransferError, match="insufficient project storage"):
        import_model_bundle(inspect_model_bundle(bundle), models_dir=tmp_path / "models")


def test_manager_reports_shared_progress_and_selection(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    choice = _gguf_choice(source)
    selected: list[tuple[LocalModelChoice, Path, str]] = []
    manager = ModelTransferManager(
        models_dir=tmp_path / "models",
        on_import_ready=lambda model, path, runtime: selected.append((model, path, runtime)),
    )
    bundle = tmp_path / "model.heartwood-model.zip"

    export_status = manager.start_export(
        choice=choice,
        model_path=source,
        bundle_path=bundle,
    )
    exported = _wait_for_transfer(manager, export_status.transfer_id)
    assert exported.status == "ready"
    assert exported.phase == "complete"
    assert exported.bytes_processed == exported.bytes_total

    plan = inspect_model_bundle(bundle)
    with pytest.raises(ModelTransferError, match="explicit approval"):
        manager.start_import(plan=plan, approved=False)
    import_status = manager.start_import(plan=plan, approved=True)
    imported = _wait_for_transfer(manager, import_status.transfer_id)

    assert imported.status == "ready"
    assert imported.result_path is not None
    assert len(selected) == 1
    assert selected[0][0].catalog_source == "transferred"


def test_manager_cancels_active_work_and_revalidates_completed_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    choice = _gguf_choice(source)
    manager = ModelTransferManager(
        models_dir=tmp_path / "models",
        on_import_ready=lambda _model, _path, _runtime: None,
    )
    bundle = tmp_path / "model.heartwood-model.zip"

    first = manager.start_export(choice=choice, model_path=source, bundle_path=bundle)
    assert _wait_for_transfer(manager, first.transfer_id).status == "ready"
    bundle.unlink()
    retry = manager.start_export(choice=choice, model_path=source, bundle_path=bundle)
    assert _wait_for_transfer(manager, retry.transfer_id).status == "ready"
    assert bundle.is_file()

    entered = Event()

    def wait_for_cancellation(
        _choice: LocalModelChoice,
        *,
        model_path: Path,
        bundle_path: Path,
        progress_callback: object | None = None,
        cancel: Event | None = None,
    ) -> Path:
        del model_path, bundle_path, progress_callback
        entered.set()
        assert cancel is not None
        assert cancel.wait(timeout=2)
        raise model_transfer_module.ModelTransferCancelledError("model transfer was cancelled")

    cancelled_bundle = tmp_path / "cancelled.zip"
    monkeypatch.setattr(model_transfer_module, "export_model_bundle", wait_for_cancellation)
    active = manager.start_export(
        choice=choice,
        model_path=source,
        bundle_path=cancelled_bundle,
    )
    assert entered.wait(timeout=2)
    assert manager.cancel(active.transfer_id).status == "cancelling"
    assert _wait_for_transfer(manager, active.transfer_id).status == "cancelled"
    assert not cancelled_bundle.exists()


def test_manager_removes_new_import_when_cancelled_before_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    choice = _gguf_choice(source)
    bundle = tmp_path / "model.zip"
    export_model_bundle(choice, model_path=source, bundle_path=bundle)
    plan = inspect_model_bundle(bundle)
    models = tmp_path / "models"
    selected: list[Path] = []
    manager = ModelTransferManager(
        models_dir=models,
        on_import_ready=lambda _choice, path, _runtime: selected.append(path),
    )

    def cancel_after_import(
        _plan: object,
        *,
        models_dir: Path,
        progress_callback: object | None = None,
        cancel: Event | None = None,
    ) -> tuple[LocalModelChoice, Path, str, bool]:
        del progress_callback
        destination = models_dir / choice.model_id
        destination.mkdir(parents=True)
        imported = destination / "model.gguf"
        imported.write_bytes(source.read_bytes())
        assert cancel is not None
        cancel.set()
        return choice, imported, "llama-cpp-cpu", True

    monkeypatch.setattr(model_transfer_module, "import_model_bundle", cancel_after_import)
    started = manager.start_import(plan=plan, approved=True)
    completed = _wait_for_transfer(manager, started.transfer_id)

    assert completed.status == "cancelled"
    assert selected == []
    assert not (models / choice.model_id).exists()


def test_bundle_paths_and_existing_destination_cannot_escape_storage(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    choice = _gguf_choice(source)
    bundle = tmp_path / "model.heartwood-model.zip"
    export_model_bundle(choice, model_path=source, bundle_path=bundle)

    unsafe = tmp_path / "unsafe.zip"
    manifest = _manifest(bundle)
    files = manifest["files"]
    assert isinstance(files, list)
    first_file = files[0]
    assert isinstance(first_file, dict)
    first_file["path"] = "../model.gguf"
    _rewrite_bundle(
        bundle,
        unsafe,
        replacements={"heartwood-model-bundle.json": _canonical_json(manifest)},
    )
    with pytest.raises(ModelTransferError, match="clean relative"):
        inspect_model_bundle(unsafe)

    outside = tmp_path / "outside"
    outside.mkdir()
    models = tmp_path / "models"
    models.mkdir()
    (models / choice.model_id).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ModelTransferError, match="escapes project model storage"):
        import_model_bundle(inspect_model_bundle(bundle), models_dir=models)


def test_gateway_restores_transferred_models_without_exposing_a_download_path(
    tmp_path: Path,
) -> None:
    connected_root = tmp_path / "connected"
    connected_root.mkdir()
    connected = SessionGateway(
        project=ProjectContext(connected_root),
        env={},
        backend_id="deterministic",
    )
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    connected.import_local_model(
        source,
        source_repository="example/transferred-model",
        source_revision="c" * 40,
        license_posture="Apache-2.0",
        context_window=32_768,
    )
    bundle = tmp_path / "transfer" / "model.zip"
    bundle.parent.mkdir()
    connected_rest = RestGateway(connected)
    exported = connected_rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/transfers/exports",
            body=json.dumps({"path": str(bundle)}),
        )
    )
    assert exported.status_code == 202
    exported_transfer_id = exported.body["transfer_id"]
    assert isinstance(exported_transfer_id, str)
    _wait_for_gateway_transfer(connected, exported_transfer_id)

    offline_root = tmp_path / "offline"
    offline_root.mkdir()
    offline = SessionGateway(
        project=ProjectContext(offline_root),
        env={},
        backend_id="deterministic",
    )
    rest = RestGateway(offline)
    inspected = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/transfers/inspect",
            body=json.dumps({"path": str(bundle)}),
        )
    )
    inspected_plan = inspect_model_bundle(bundle)
    rejected = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/transfers/imports",
            body=json.dumps(
                {
                    "path": str(bundle),
                    "approved": False,
                    "manifest_sha256": inspected_plan.manifest_sha256,
                }
            ),
        )
    )
    accepted = rest.handle(
        RestRequest(
            method="POST",
            path="/settings/models/transfers/imports",
            body=json.dumps(
                {
                    "path": str(bundle),
                    "approved": True,
                    "manifest_sha256": inspected_plan.manifest_sha256,
                }
            ),
        )
    )

    assert inspected.status_code == 200
    assert isinstance(inspected.body, dict)
    inspected_model = inspected.body["model"]
    assert isinstance(inspected_model, dict)
    assert inspected_model["catalog_source"] == "transferred"
    assert rejected.status_code == 422
    assert accepted.status_code == 202
    missing_cancel = rest.handle(
        RestRequest(
            method="DELETE",
            path="/settings/models/transfers/unknown-transfer",
        )
    )
    assert missing_cancel.status_code == 404
    transfer_id = accepted.body["transfer_id"]
    assert isinstance(transfer_id, str)
    completed = _wait_for_gateway_transfer(offline, transfer_id)
    assert completed["status"] == "ready"
    selected = offline.config_store.load().local_model
    assert selected is not None
    assert selected.catalog_source == "transferred"

    restarted = SessionGateway(
        project=ProjectContext(offline_root),
        env={},
        backend_id="deterministic",
    )
    transferred = next(
        model for model in restarted.model_artifacts()["models"] if model["selected"] is True
    )
    assert transferred["catalog_source"] == "transferred"
    with pytest.raises(ModelRepositoryError, match="unknown Heartwood-managed model"):
        restarted.download_local_model(str(transferred["model_id"]))


def test_bundle_cannot_assign_itself_platform_qualification(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    claimed = replace(
        _gguf_choice(source),
        qualification="qualified",
        validated_platforms=("generic",),
        qualification_test="heartwood.untrusted-claim.v1",
        qualification_date="2026-08-03",
        qualification_evidence="https://example.invalid/untrusted",
    )
    bundle = tmp_path / "model.heartwood-model.zip"
    export_model_bundle(claimed, model_path=source, bundle_path=bundle)
    project = tmp_path / "offline"
    project.mkdir()
    gateway = SessionGateway(
        project=ProjectContext(project),
        env={},
        backend_id="deterministic",
    )

    plan = gateway.inspect_local_model_bundle(bundle)
    assert plan["model"]["qualification"] == "unvalidated"
    assert any("has not completed Heartwood qualification" in item for item in plan["warnings"])
    started = gateway.import_local_model_bundle(
        bundle,
        approved=True,
        manifest_sha256=plan["manifest_sha256"],
    )
    completed = _wait_for_gateway_transfer(gateway, started["transfer_id"])
    assert completed["status"] == "ready", completed["error"]
    selected = gateway.config_store.load().local_model
    assert selected is not None
    assert selected.qualification == "unvalidated"
    assert selected.validated_platforms == ()


def test_gateway_rejects_a_bundle_changed_after_license_review(tmp_path: Path) -> None:
    source = tmp_path / "source.gguf"
    source.write_bytes(b"GGUFsynthetic-transfer-model")
    bundle = tmp_path / "model.heartwood-model.zip"
    export_model_bundle(_gguf_choice(source), model_path=source, bundle_path=bundle)
    project = tmp_path / "offline"
    project.mkdir()
    gateway = SessionGateway(
        project=ProjectContext(project),
        env={},
        backend_id="deterministic",
    )
    reviewed = gateway.inspect_local_model_bundle(bundle)
    changed_manifest = _manifest(bundle)
    model = changed_manifest["model"]
    assert isinstance(model, dict)
    model["license_posture"] = "Different license review"
    changed = tmp_path / "changed.zip"
    _rewrite_bundle(
        bundle,
        changed,
        replacements={"heartwood-model-bundle.json": _canonical_json(changed_manifest)},
    )
    changed.replace(bundle)

    with pytest.raises(ModelTransferError, match="changed after review"):
        gateway.import_local_model_bundle(
            bundle,
            approved=True,
            manifest_sha256=reviewed["manifest_sha256"],
        )


def _gguf_choice(path: Path) -> LocalModelChoice:
    content = path.read_bytes()
    return LocalModelChoice(
        model_id="synthetic-transfer-model",
        label="Synthetic transfer model",
        purpose="Synthetic transfer verification",
        runtime="llama-cpp",
        source_repository="example/synthetic-transfer-model",
        source_revision="a" * 40,
        source_path="model.gguf",
        size_bytes=len(content),
        minimum_free_bytes=len(content),
        license_posture="Apache-2.0",
        catalog_source="catalog",
        context_window=32_768,
        artifact_sha256=hashlib.sha256(content).hexdigest(),
        minimum_resource_envelope="Synthetic transfer test resources.",
        recommended_resource_envelope="Synthetic transfer test resources.",
        license_id="Apache-2.0",
        precision="Q4_K_M",
        recommended_ram_bytes=1_024,
        recommended_disk_bytes=1_024,
        maximum_context_window=32_768,
        recommended_cpu_count=1,
    )


def _vllm_snapshot(path: Path) -> Path:
    path.mkdir()
    (path / "config.json").write_text(
        json.dumps({"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3"}),
        encoding="utf-8",
    )
    (path / "model.safetensors").write_bytes(b"synthetic-safetensors")
    entries = []
    for item in sorted(path.iterdir()):
        entries.append(f"{hashlib.sha256(item.read_bytes()).hexdigest()}  {item.name}")
    (path / "SHA256SUMS").write_text("\n".join(entries) + "\n", encoding="utf-8")
    return path


def _vllm_choice(path: Path) -> LocalModelChoice:
    size = sum(item.stat().st_size for item in path.iterdir() if item.is_file())
    return LocalModelChoice(
        model_id="synthetic-vllm-transfer-model",
        label="Synthetic vLLM transfer model",
        purpose="Synthetic GPU transfer verification",
        runtime="vllm",
        source_repository="example/synthetic-vllm-transfer-model",
        source_revision="b" * 40,
        source_path=None,
        size_bytes=size,
        minimum_free_bytes=size,
        license_posture="Apache-2.0",
        catalog_source="catalog",
        model_type="qwen3",
        context_window=32_768,
        license_id="Apache-2.0",
        precision="BF16",
        minimum_gpu_count=1,
        minimum_gpu_memory_bytes=1,
        recommended_ram_bytes=1_024,
        recommended_disk_bytes=max(size, 1_024),
        maximum_context_window=32_768,
        tool_call_parser="hermes",
        tensor_parallel_size=1,
        download_policy="synthetic",
        allow_patterns=("*.json", "*.safetensors"),
        recommended_cpu_count=1,
    )


def _wait_for_transfer(manager: ModelTransferManager, transfer_id: str) -> ModelTransfer:
    deadline = time.monotonic() + 5
    status = manager.status(transfer_id)
    while status.status in {"running", "cancelling"} and time.monotonic() < deadline:
        time.sleep(0.01)
        status = manager.status(transfer_id)
    return status


def _wait_for_gateway_transfer(
    gateway: SessionGateway,
    transfer_id: str,
) -> ModelTransferResponse:
    deadline = time.monotonic() + 5
    status = gateway.model_transfer_status(transfer_id)
    while status["status"] in {"running", "cancelling"} and time.monotonic() < deadline:
        time.sleep(0.01)
        status = gateway.model_transfer_status(transfer_id)
    return status


def _manifest(path: Path) -> dict[str, JsonValue]:
    with ZipFile(path) as archive:
        return cast(
            dict[str, JsonValue],
            json.loads(archive.read("heartwood-model-bundle.json")),
        )


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _rewrite_bundle(
    source: Path,
    destination: Path,
    *,
    replacements: dict[str, bytes] | None = None,
    dropped: set[str] | None = None,
) -> None:
    replacements = replacements or {}
    dropped = dropped or set()
    with ZipFile(source) as original, ZipFile(destination, "w", compression=ZIP_STORED) as output:
        for info in original.infolist():
            if info.filename not in dropped:
                output.writestr(info.filename, replacements.get(info.filename, original.read(info)))
