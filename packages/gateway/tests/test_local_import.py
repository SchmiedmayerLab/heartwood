# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from heartwood.gateway import (
    ModelRepositoryError,
    ProjectContext,
    RestGateway,
    RestRequest,
    SessionGateway,
    import_local_model,
    verify_model_snapshot,
)


def _gguf(path: Path) -> Path:
    path.write_bytes(b"GGUFsynthetic-model")
    return path


def test_imports_gguf_atomically_with_provenance_and_integrity(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    source = _gguf(tmp_path / "model.gguf")

    imported = import_local_model(
        source,
        models_dir=models,
        source_repository="example/research-model-gguf",
        source_revision="1" * 40,
        license_posture="Apache-2.0",
    )

    assert imported.path.read_bytes() == source.read_bytes()
    assert imported.path != source
    assert imported.model.runtime == "llama-cpp"
    assert imported.model.artifact_sha256 is not None
    manifest = json.loads((imported.path.parent / "heartwood-model.json").read_text())
    assert manifest["source_repository"] == "example/research-model-gguf"
    assert str(source) not in json.dumps(manifest)


def test_imports_supported_vllm_snapshot_and_rejects_executable_code(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text(
        json.dumps({"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3"}),
        encoding="utf-8",
    )
    (snapshot / "model.safetensors").write_bytes(b"synthetic")

    imported = import_local_model(
        snapshot,
        models_dir=models,
        source_repository="example/research-model",
        source_revision="2" * 40,
        license_posture="Apache-2.0",
    )

    assert imported.model.runtime == "vllm"
    assert imported.model.model_type == "qwen3"
    assert (imported.path / "model.safetensors").is_file()
    provenance = json.loads((imported.path / "heartwood-model.json").read_text())
    assert provenance["model_type"] == "qwen3"
    verify_model_snapshot(imported.path)

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    (unsafe / "config.json").write_text(
        json.dumps({"architectures": ["Unsafe"], "auto_map": {"AutoModel": "model.py"}}),
        encoding="utf-8",
    )
    (unsafe / "model.safetensors").write_bytes(b"synthetic")
    (unsafe / "model.py").write_text("raise RuntimeError\n", encoding="utf-8")
    with pytest.raises(ModelRepositoryError, match="remote code"):
        import_local_model(
            unsafe,
            models_dir=models,
            source_repository="example/unsafe-model",
            source_revision="3" * 40,
            license_posture="Unknown",
        )


def test_import_rejects_symlinks_and_requires_immutable_provenance(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    source = _gguf(tmp_path / "model.gguf")
    linked = tmp_path / "linked.gguf"
    linked.symlink_to(source)

    with pytest.raises(ModelRepositoryError, match="symbolic links"):
        import_local_model(
            linked,
            models_dir=models,
            source_repository="example/model",
            source_revision="4" * 40,
            license_posture="Apache-2.0",
        )
    with pytest.raises(ModelRepositoryError, match="immutable commit"):
        import_local_model(
            source,
            models_dir=models,
            source_repository="example/model",
            source_revision="main",
            license_posture="Apache-2.0",
        )

    with pytest.raises(ModelRepositoryError, match="at least 18,432 tokens"):
        import_local_model(
            source,
            models_dir=models,
            source_repository="example/model",
            source_revision="4" * 40,
            license_posture="Apache-2.0",
            context_window=4_096,
        )


@pytest.mark.parametrize("source_location", ["equal", "ancestor", "descendant"])
def test_import_rejects_source_paths_that_overlap_project_model_storage(
    tmp_path: Path,
    source_location: str,
) -> None:
    models = tmp_path / "models"
    models.mkdir()
    if source_location == "equal":
        source = models
    elif source_location == "ancestor":
        source = tmp_path
        (source / "config.json").write_text(
            json.dumps({"architectures": ["SyntheticForCausalLM"]}),
            encoding="utf-8",
        )
        (source / "model.safetensors").write_bytes(b"synthetic")
    else:
        source = models / "existing-snapshot"
        source.mkdir()
    with pytest.raises(ModelRepositoryError, match="must be separate paths"):
        import_local_model(
            source,
            models_dir=models,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
        )


def test_import_rejects_invalid_identity_license_and_source(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    source = _gguf(tmp_path / "model.gguf")

    with pytest.raises(ModelRepositoryError, match="owner/model"):
        import_local_model(
            source,
            models_dir=models,
            source_repository="model-only",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
        )
    with pytest.raises(ModelRepositoryError, match="license"):
        import_local_model(
            source,
            models_dir=models,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture=" ",
        )
    with pytest.raises(ModelRepositoryError, match="does not exist"):
        import_local_model(
            tmp_path / "missing.gguf",
            models_dir=models,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
        )


def test_import_rejects_unsupported_and_malformed_model_artifacts(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()

    text_model = tmp_path / "model.bin"
    text_model.write_bytes(b"GGUFsynthetic")
    with pytest.raises(ModelRepositoryError, match="GGUF format"):
        import_local_model(
            text_model,
            models_dir=models,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
        )

    malformed_gguf = tmp_path / "model.gguf"
    malformed_gguf.write_bytes(b"not-a-gguf")
    with pytest.raises(ModelRepositoryError, match="GGUF header"):
        import_local_model(
            malformed_gguf,
            models_dir=models,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
        )


@pytest.mark.parametrize(
    ("config", "add_weights", "add_python", "message"),
    [
        (None, False, False, "contain config.json"),
        ("{", True, False, "config.json is invalid"),
        (json.dumps({}), True, False, "declare its architecture"),
        (json.dumps({"architectures": ["Synthetic"]}), False, False, "safetensors"),
        (json.dumps({"architectures": ["Synthetic"]}), True, True, "executable Python"),
    ],
)
def test_import_rejects_incomplete_or_executable_vllm_snapshots(
    tmp_path: Path,
    config: str | None,
    add_weights: bool,
    add_python: bool,
    message: str,
) -> None:
    models = tmp_path / "models"
    models.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    if config is not None:
        (snapshot / "config.json").write_text(config, encoding="utf-8")
    if add_weights:
        (snapshot / "model.safetensors").write_bytes(b"synthetic")
    if add_python:
        (snapshot / "modeling.py").write_text("raise RuntimeError\n", encoding="utf-8")

    with pytest.raises(ModelRepositoryError, match=message):
        import_local_model(
            snapshot,
            models_dir=models,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
        )


def test_import_rejects_nested_symlinks_and_insufficient_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = tmp_path / "models"
    models.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "linked").symlink_to(tmp_path / "outside")
    with pytest.raises(ModelRepositoryError, match="must not contain symbolic links"):
        import_local_model(
            snapshot,
            models_dir=models,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
        )

    source = _gguf(tmp_path / "model.gguf")
    monkeypatch.setattr(
        "heartwood.gateway._local_import.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    with pytest.raises(ModelRepositoryError, match="insufficient project storage"):
        import_local_model(
            source,
            models_dir=models,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
        )


def test_import_rejects_duplicates_and_cleans_up_failed_copies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = tmp_path / "models"
    models.mkdir()
    source = _gguf(tmp_path / "model.gguf")
    kwargs = {
        "models_dir": models,
        "source_repository": "example/model",
        "source_revision": "1" * 40,
        "license_posture": "Apache-2.0",
    }
    import_local_model(source, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ModelRepositoryError, match="already imported"):
        import_local_model(source, **kwargs)  # type: ignore[arg-type]

    second = _gguf(tmp_path / "second.gguf")

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic copy failure")

    monkeypatch.setattr("heartwood.gateway._local_import.shutil.copy2", fail_copy)
    with pytest.raises(OSError, match="synthetic copy failure"):
        import_local_model(
            second,
            models_dir=models,
            source_repository="example/second",
            source_revision="2" * 40,
            license_posture="Apache-2.0",
        )
    assert not tuple(models.glob(".imported-*"))


def test_import_validates_metadata_before_committing_large_artifacts(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    source = _gguf(tmp_path / "model.gguf")

    with pytest.raises(ModelRepositoryError, match="context window"):
        import_local_model(
            source,
            models_dir=models,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
            context_window=1,
        )

    assert not tuple(models.iterdir())


def test_gateway_removes_import_when_project_selection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    source = _gguf(tmp_path / "model.gguf")
    gateway = SessionGateway(project=ProjectContext(project_root), env={})

    def fail_selection(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic selection failure")

    monkeypatch.setattr(gateway, "_select_downloaded_local_model", fail_selection)

    with pytest.raises(RuntimeError, match="synthetic selection failure"):
        gateway.import_local_model(
            source,
            source_repository="example/model",
            source_revision="1" * 40,
            license_posture="Apache-2.0",
            context_window=32_768,
        )

    assert not tuple(gateway.project.models_dir.glob("imported-*"))
    assert not gateway.config_store.configured


def test_rest_import_selects_the_model_without_exposing_the_source_path(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    source = _gguf(tmp_path / "outside.gguf")
    gateway = SessionGateway(project=ProjectContext(project_root), env={})
    response = RestGateway(gateway).handle(
        RestRequest(
            method="POST",
            path="/settings/models/imports",
            body=json.dumps(
                {
                    "path": str(source),
                    "repository": "example/imported-model",
                    "revision": "5" * 40,
                    "license": "Apache-2.0",
                }
            ),
        )
    )

    assert response.status_code == 201
    assert response.body["status"] == "ready"
    config = gateway.config_store.load()
    assert config.model_source == "heartwood"
    assert config.local_model is not None
    assert config.local_model.source_repository == "example/imported-model"
    assert str(source) not in project_root.joinpath(".heartwood/config.toml").read_text()
