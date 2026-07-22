# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from heartwood.gateway import (
    ModelSnapshot,
    ModelSnapshotCatalog,
    ModelSnapshotError,
    automatic_model_tier,
    download_model_snapshot,
    load_model_snapshot_catalog,
    plan_local_context_window,
    verify_model_snapshot,
)


@pytest.mark.parametrize(
    (
        "snapshot_id",
        "repository",
        "revision",
        "tier",
        "gpu_count",
        "tool_parser",
    ),
    [
        (
            "qwen25-coder-7b-instruct-awq-vllm",
            "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ",
            "8e8ed243bbe6f9a5aff549a0924562fc719b2b8a",
            "standard",
            1,
            "hermes",
        ),
        (
            "qwen25-coder-14b-instruct-awq-vllm",
            "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ",
            "eb3172f06a6d6b3a15f08947b0668d782e4d2d2c",
            "powerful",
            1,
            "hermes",
        ),
        (
            "qwen3-coder-30b-a3b-instruct-fp8-vllm",
            "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8",
            "dcaee4d4dfc5ee71ad501f01f530e5652438fde0",
            "powerful",
            1,
            "qwen3_coder",
        ),
        (
            "qwen3-coder-next-fp8-vllm",
            "Qwen/Qwen3-Coder-Next-FP8",
            "da6e2ed27304dd39abadd9c82ef50e8de67bdd4c",
            "maximum",
            4,
            "qwen3_coder",
        ),
        (
            "qwen3-coder-30b-a3b-instruct-w4a16-awq-vllm",
            "YCWTG/Qwen3-Coder-30B-A3B-Instruct-W4A16-mixed-AWQ",
            "e69e73813144d9b715648d8384b3f2c035397411",
            "powerful",
            2,
            "qwen3_coder",
        ),
        (
            "gpt-oss-20b-vllm",
            "openai/gpt-oss-20b",
            "6cee5e81ee83917806bbde320786a8fb61efebee",
            "powerful",
            4,
            "openai",
        ),
        (
            "gpt-oss-120b-vllm",
            "openai/gpt-oss-120b",
            "b5c939de8f754692c1647ca79fbf85e8c1e70f8a",
            "maximum",
            2,
            "openai",
        ),
    ],
)
def test_repository_snapshot_catalog_pins_gpu_model_variants(
    snapshot_id: str,
    repository: str,
    revision: str,
    tier: str,
    gpu_count: int,
    tool_parser: str,
) -> None:
    catalog = load_model_snapshot_catalog(
        _repo_root() / "images" / "generic" / "local-runtime" / "snapshots.toml"
    )

    snapshot = catalog.snapshot(snapshot_id)
    assert snapshot.runtime_profile == "vllm-cuda"
    assert snapshot.source_repository == repository
    assert snapshot.source_revision == revision
    assert snapshot.tier == tier
    assert snapshot.tensor_parallel_size == gpu_count
    assert snapshot.tool_call_parser == tool_parser
    assert snapshot.minimum_free_bytes >= snapshot.expected_size_bytes
    assert snapshot.recommended_disk_bytes >= snapshot.minimum_free_bytes
    assert snapshot.context_window <= snapshot.maximum_context_window
    expected_qualification = (
        "qualified"
        if snapshot_id
        in {
            "qwen3-coder-30b-a3b-instruct-fp8-vllm",
        }
        else "candidate"
    )
    assert snapshot.qualification == expected_qualification
    if expected_qualification == "qualified":
        expected_platform = (
            "carina" if snapshot_id == "qwen3-coder-30b-a3b-instruct-fp8-vllm" else "terra"
        )
        assert snapshot.validated_platforms == (expected_platform,)
        assert snapshot.qualification_test == "heartwood.coding-agent-e2e.v1"
        assert snapshot.recommended is True
    else:
        assert snapshot.validated_platforms == ()
        assert snapshot.qualification_test is None
        assert snapshot.recommended is False


@pytest.mark.parametrize(
    ("platform_id", "tier"),
    [
        ("generic", "standard"),
        ("terra", "maximum"),
        ("carina", "powerful"),
        ("custom", "standard"),
    ],
)
def test_automatic_model_tier_is_shared_across_interfaces(
    platform_id: str,
    tier: str,
) -> None:
    assert automatic_model_tier(platform_id) == tier


@pytest.mark.parametrize(
    ("snapshot_id", "minimum_vram_gib"),
    [
        ("qwen25-coder-7b-instruct-awq-vllm", 16),
    ],
)
def test_snapshot_minimum_vram_supports_its_advertised_context(
    snapshot_id: str,
    minimum_vram_gib: int,
) -> None:
    catalog = load_model_snapshot_catalog(
        _repo_root() / "images" / "generic" / "local-runtime" / "snapshots.toml"
    )
    snapshot = catalog.snapshot(snapshot_id)

    plan = plan_local_context_window(
        model_limit=snapshot.context_window,
        model_size_bytes=snapshot.expected_size_bytes,
        runtime="vllm",
        available_memory_bytes=minimum_vram_gib * 1024**3,
    )

    assert plan.effective_window == snapshot.context_window
    assert f"{minimum_vram_gib} GB VRAM" in str(snapshot.minimum_resource_envelope)


def test_catalog_recommends_only_qualified_models_with_compatible_resources() -> None:
    source = load_model_snapshot_catalog(
        _repo_root() / "images" / "generic" / "local-runtime" / "snapshots.toml"
    )
    terra_candidate = source.snapshot("qwen25-coder-14b-instruct-awq-vllm")
    powerful = replace(
        source.snapshot("qwen3-coder-30b-a3b-instruct-fp8-vllm"),
        qualification="qualified",
        validated_platforms=("carina",),
        qualification_test="heartwood.coding-agent-e2e.v1",
        recommended=True,
    )
    catalog = ModelSnapshotCatalog(
        source.schema_version,
        (terra_candidate, powerful),
    )

    assert (
        catalog.recommend(
            platform_id="terra",
            gpu_count=1,
            gpu_memory_bytes=16_000_000_000,
            maximum_tier="maximum",
        )
        is None
    )
    assert (
        catalog.recommend(
            platform_id="carina",
            gpu_count=1,
            gpu_memory_bytes=48_000_000_000,
            maximum_tier="powerful",
        )
        == powerful
    )
    assert (
        catalog.recommend(
            platform_id="carina",
            gpu_count=1,
            gpu_memory_bytes=16_000_000_000,
            maximum_tier="powerful",
        )
        is None
    )
    assert (
        source.recommend(
            platform_id="terra",
            gpu_count=1,
            gpu_memory_bytes=16_000_000_000,
            maximum_tier="maximum",
        )
        is None
    )

    assert (
        catalog.recommend_for_capacities(
            platform_id="terra",
            capacities=((1, 16_000_000_000), (1, 48_000_000_000)),
            maximum_tier="maximum",
        )
        is None
    )
    assert (
        catalog.recommend_for_capacities(
            platform_id="generic",
            capacities=((4, 48_000_000_000),),
            maximum_tier="maximum",
        )
        is None
    )


def test_catalog_capacity_recommendation_prefers_more_parallelism_within_tier() -> None:
    source = load_model_snapshot_catalog(
        _repo_root() / "images" / "generic" / "local-runtime" / "snapshots.toml"
    )
    single = replace(
        source.snapshot("qwen3-coder-30b-a3b-instruct-fp8-vllm"),
        qualification="qualified",
        validated_platforms=("carina",),
        qualification_test="heartwood.coding-agent-e2e.v1",
        recommended=True,
    )
    dual = replace(
        single,
        snapshot_id="synthetic-dual-gpu-model",
        minimum_gpu_count=2,
        tensor_parallel_size=2,
    )
    catalog = ModelSnapshotCatalog(source.schema_version, (single, dual))

    recommendation = catalog.recommend_for_capacities(
        platform_id="carina",
        capacities=((1, 48_000_000_000), (2, 48_000_000_000)),
        maximum_tier="powerful",
    )

    assert recommendation == dual


def test_snapshot_download_is_atomic_and_creates_exact_provenance(tmp_path: Path) -> None:
    snapshot = _snapshot()
    progress: list[tuple[int, int]] = []

    def downloader(**kwargs: object) -> str:
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "config.json").write_text("{}\n", encoding="utf-8")
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        (local_dir / ".cache" / "huggingface").mkdir(parents=True)
        (local_dir / ".cache" / "huggingface" / "download.lock").touch()
        return str(local_dir)

    destination = download_model_snapshot(
        snapshot,
        cache_dir=tmp_path / "models",
        downloader=downloader,
        progress_callback=lambda downloaded, total: progress.append((downloaded, total)),
    )

    assert destination == tmp_path / "models" / snapshot.snapshot_id
    assert not (destination / ".cache").exists()
    source = json.loads((destination / "HEARTWOOD-SOURCE.json").read_text(encoding="utf-8"))
    assert source["source_revision"] == snapshot.source_revision
    verify_model_snapshot(destination)
    assert (tmp_path / "models" / f".{snapshot.snapshot_id}.lock").is_file()
    assert not any(
        path.is_dir() for path in (tmp_path / "models").glob(f".{snapshot.snapshot_id}.*")
    )
    assert progress[0] == (0, snapshot.expected_size_bytes)
    assert progress[-1] == (snapshot.expected_size_bytes, snapshot.expected_size_bytes)


def test_snapshot_download_reuses_verified_content_and_rejects_tampering(tmp_path: Path) -> None:
    snapshot = _snapshot()
    calls = 0

    def downloader(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        return str(local_dir)

    destination = download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)
    reused = download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)
    assert reused == destination
    assert calls == 1

    (destination / "weights.safetensors").write_bytes(b"modified")
    with pytest.raises(ModelSnapshotError, match="incomplete or modified"):
        download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)


def test_snapshot_download_serializes_concurrent_callers(tmp_path: Path) -> None:
    snapshot = _snapshot()
    started = Event()
    release = Event()
    calls = 0

    def downloader(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        started.set()
        assert release.wait(timeout=5)
        return str(local_dir)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            download_model_snapshot,
            snapshot,
            cache_dir=tmp_path,
            downloader=downloader,
        )
        assert started.wait(timeout=5)
        second = executor.submit(
            download_model_snapshot,
            snapshot,
            cache_dir=tmp_path,
            downloader=downloader,
        )
        release.set()

        assert first.result(timeout=5) == second.result(timeout=5)
    assert calls == 1


def test_snapshot_download_rejects_an_existing_different_revision(tmp_path: Path) -> None:
    snapshot = _snapshot()

    def downloader(**kwargs: object) -> str:
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        return str(local_dir)

    download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)
    different_revision = replace(snapshot, source_revision="f" * 40)

    with pytest.raises(ModelSnapshotError, match="incomplete or modified"):
        download_model_snapshot(
            different_revision,
            cache_dir=tmp_path,
            downloader=downloader,
        )


def test_snapshot_download_rejects_content_outside_reviewed_size(tmp_path: Path) -> None:
    snapshot = replace(_snapshot(), expected_size_bytes=10, minimum_free_bytes=10)

    def downloader(**kwargs: object) -> str:
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"x" * 100)
        return str(local_dir)

    with pytest.raises(ModelSnapshotError, match="outside the reviewed range"):
        download_model_snapshot(snapshot, cache_dir=tmp_path, downloader=downloader)
    assert not any(path.is_dir() for path in tmp_path.glob(f".{snapshot.snapshot_id}.*"))


def test_snapshot_metadata_rejects_floating_revisions() -> None:
    with pytest.raises(ModelSnapshotError, match="immutable commit revision"):
        replace(_snapshot(), source_revision="main").validate()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"snapshot_id": "../unsafe"}, "safe cache directory"),
        ({"source_repository": "not-a-repository"}, "owner/repository"),
        ({"purpose": ""}, "purpose must be"),
        ({"expected_size_bytes": 0}, "storage metadata"),
        ({"minimum_free_bytes": 1}, "storage metadata"),
        ({"recommended_disk_bytes": 19}, "must cover minimum_free_bytes"),
        ({"recommended_ram_bytes": 0}, "recommended_ram_bytes must be positive"),
        ({"minimum_gpu_count": 0}, "GPU resource metadata must be positive"),
        ({"minimum_gpu_memory_bytes": 0}, "GPU resource metadata must be positive"),
        ({"tensor_parallel_size": 0}, "must cover the minimum GPU count"),
        ({"tier": "unknown"}, "unsupported model tier"),
        ({"qualification": "unknown"}, "unsupported model qualification"),
        ({"tool_call_parser": "unknown"}, "unsupported vLLM tool-call parser"),
        ({"startup_seconds_min": 0}, "startup estimate is invalid"),
        ({"startup_seconds_max": 0}, "startup estimate is invalid"),
        ({"context_window": 2047}, "between 2048 and 1048576"),
        ({"context_window": 1_048_577}, "between 2048 and 1048576"),
        ({"maximum_context_window": 1024}, "must cover the default context window"),
        ({"maximum_context_window": 1_048_577}, "must cover the default context window"),
        ({"allow_patterns": ()}, "must select reviewed snapshot files"),
        ({"allow_patterns": ("*.json", "*.json")}, "must not contain duplicates"),
        ({"ignore_patterns": ("*.bin", "*.bin")}, "must not contain duplicates"),
        ({"allow_patterns": ("../*.json",)}, "unsafe repository pattern"),
        ({"ignore_patterns": ("/tmp/*",)}, "unsafe repository pattern"),
        ({"validated_platforms": ("terra", "terra")}, "must not contain duplicates"),
        ({"validated_platforms": ("unknown",)}, "unsupported platform"),
        ({"qualification": "qualified"}, "require validated platforms"),
        (
            {"qualification": "qualified", "validated_platforms": ("terra",)},
            "require validated platforms",
        ),
        ({"recommended": True}, "candidate models cannot be recommended"),
    ],
)
def test_snapshot_metadata_rejects_unsafe_values(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ModelSnapshotError, match=message):
        replace(_snapshot(), **changes).validate()  # type: ignore[arg-type]


def test_snapshot_catalog_reports_unknown_ids_and_invalid_documents(tmp_path: Path) -> None:
    catalog = load_model_snapshot_catalog(
        _repo_root() / "images" / "generic" / "local-runtime" / "snapshots.toml"
    )
    with pytest.raises(ModelSnapshotError, match="unknown model snapshot"):
        catalog.snapshot("missing")

    missing = tmp_path / "missing.toml"
    with pytest.raises(ModelSnapshotError, match="unable to load"):
        load_model_snapshot_catalog(missing)

    invalid_schema = tmp_path / "schema.toml"
    invalid_schema.write_text('schema_version = "unsupported"\n[snapshots]\n', encoding="utf-8")
    with pytest.raises(ModelSnapshotError, match="unsupported"):
        load_model_snapshot_catalog(invalid_schema)

    missing_snapshots = tmp_path / "missing-snapshots.toml"
    missing_snapshots.write_text(
        'schema_version = "heartwood.model-snapshot-catalog.v2"\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelSnapshotError, match="snapshots table"):
        load_model_snapshot_catalog(missing_snapshots)

    missing_policies = tmp_path / "missing-policies.toml"
    missing_policies.write_text(
        'schema_version = "heartwood.model-snapshot-catalog.v2"\n[snapshots]\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelSnapshotError, match="download policies"):
        load_model_snapshot_catalog(missing_policies)

    invalid_policy = tmp_path / "invalid-policy.toml"
    invalid_policy.write_text(
        'schema_version = "heartwood.model-snapshot-catalog.v2"\n'
        '[download_policies]\ninvalid = "value"\n[snapshots]\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelSnapshotError, match="policy entries must be tables"):
        load_model_snapshot_catalog(invalid_policy)

    invalid_entry = tmp_path / "entry.toml"
    invalid_entry.write_text(
        'schema_version = "heartwood.model-snapshot-catalog.v2"\n'
        "[download_policies.safe]\n"
        'allow_patterns = ["*.json"]\n'
        "[snapshots]\n"
        'invalid = "value"\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelSnapshotError, match="entries must be tables"):
        load_model_snapshot_catalog(invalid_entry)

    invalid_fields = tmp_path / "fields.toml"
    invalid_fields.write_text(
        'schema_version = "heartwood.model-snapshot-catalog.v2"\n'
        "[download_policies.safe]\n"
        'allow_patterns = ["*.json"]\n'
        "[snapshots.invalid]\n"
        'runtime_profile = ""\n',
        encoding="utf-8",
    )
    with pytest.raises(ModelSnapshotError, match="download_policy"):
        load_model_snapshot_catalog(invalid_fields)


def test_snapshot_download_checks_capacity_and_uses_hugging_face_downloader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot()
    monkeypatch.setattr(
        "heartwood.gateway._model_snapshots.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=1),
    )
    with pytest.raises(ModelSnapshotError, match="requires at least"):
        download_model_snapshot(snapshot, cache_dir=tmp_path / "small")

    monkeypatch.setattr(
        "heartwood.gateway._model_snapshots.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=100, used=0, free=100),
    )
    calls: list[dict[str, object]] = []

    def snapshot_download(**kwargs: object) -> str:
        calls.append(kwargs)
        local_dir = Path(str(kwargs["local_dir"]))
        (local_dir / "weights.safetensors").write_bytes(b"synthetic-weights")
        return str(local_dir)

    monkeypatch.setattr(
        "heartwood.gateway._model_snapshots.import_module",
        lambda _name: SimpleNamespace(snapshot_download=snapshot_download),
    )

    destination = download_model_snapshot(snapshot, cache_dir=tmp_path / "enough")

    assert destination.is_dir()
    assert calls[0]["repo_id"] == snapshot.source_repository
    assert calls[0]["revision"] == snapshot.source_revision
    local_dir = Path(str(calls[0]["local_dir"]))
    assert calls[0]["cache_dir"] == local_dir / ".cache" / "huggingface"
    assert calls[0]["token"] is False


def test_snapshot_download_rejects_existing_content_without_source_record(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    destination = tmp_path / snapshot.snapshot_id
    destination.mkdir()
    content = b"synthetic-weights"
    (destination / "weights.safetensors").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    (destination / "SHA256SUMS").write_text(f"{digest}  weights.safetensors\n", encoding="utf-8")

    with pytest.raises(ModelSnapshotError, match="incomplete or modified"):
        download_model_snapshot(snapshot, cache_dir=tmp_path)


def test_snapshot_verifier_rejects_a_linked_root(tmp_path: Path) -> None:
    root = tmp_path / "real"
    root.mkdir()
    (root / "file").write_text("content", encoding="utf-8")
    (root / "SHA256SUMS").write_text(
        "ed7002b439e9ac845f22357d822bac144473aa7c2d8b7b3e36726061b4a93f03  file\n",
        encoding="utf-8",
    )
    linked = tmp_path / "linked"
    linked.symlink_to(root, target_is_directory=True)

    with pytest.raises(ValueError, match="regular SHA256SUMS"):
        verify_model_snapshot(linked)


def _snapshot() -> ModelSnapshot:
    return ModelSnapshot(
        snapshot_id="synthetic-vllm",
        runtime_profile="vllm-cuda",
        purpose="Synthetic test snapshot.",
        source_repository="example/model",
        source_revision="0123456789abcdef0123456789abcdef01234567",
        expected_size_bytes=20,
        minimum_free_bytes=20,
        license_id="Apache-2.0",
        license_posture="Synthetic test content only.",
        model_alias="Synthetic vLLM",
        precision="Synthetic",
        tier="standard",
        qualification="candidate",
        minimum_gpu_count=1,
        minimum_gpu_memory_bytes=1,
        recommended_ram_bytes=1,
        recommended_disk_bytes=20,
        maximum_context_window=32_768,
        tool_call_parser="hermes",
        tensor_parallel_size=1,
        startup_seconds_min=1,
        startup_seconds_max=2,
        download_policy="synthetic",
        allow_patterns=("*.json", "*.safetensors"),
        ignore_patterns=("*.bin",),
        context_window=32_768,
    )


def _repo_root() -> Path:
    return Path(__file__).parents[3]
