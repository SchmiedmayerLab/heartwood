# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Recommended multi-file model snapshots for native inference runtimes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

from filelock import FileLock

from heartwood.gateway._local_model_contract import (
    DEFAULT_LOCAL_CONTEXT_WINDOW,
    MAXIMUM_LOCAL_CONTEXT_WINDOW,
    MINIMUM_LOCAL_CONTEXT_WINDOW,
)
from heartwood.gateway._model_identity import (
    is_hugging_face_model_id,
    is_resolved_revision,
)

_ENTRY = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")
_SNAPSHOT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._*?/[\]-]+$")
_SIZE_TOLERANCE = 0.20

type ProgressCallback = Callable[[int, int], None]
type ModelTier = Literal["standard", "powerful", "maximum"]
type ModelQualification = Literal["unvalidated", "qualified"]
type ToolCallParser = Literal["hermes", "openai", "qwen3_coder"]

_MODEL_TIERS = {"standard", "powerful", "maximum"}
_MODEL_TIER_RANK: dict[str, int] = {"standard": 0, "powerful": 1, "maximum": 2}
_MODEL_QUALIFICATIONS = {"unvalidated", "qualified"}
_TOOL_CALL_PARSERS = {"hermes", "openai", "qwen3_coder"}
_VALIDATED_PLATFORMS = {"carina", "generic", "terra"}


def automatic_model_tier(platform_id: str) -> ModelTier:
    """Return the highest tier considered by automatic model selection."""
    if platform_id == "terra":
        return "maximum"
    if platform_id == "carina":
        return "powerful"
    return "standard"


class SnapshotDownloader(Protocol):
    """Callable contract implemented by ``huggingface_hub.snapshot_download``."""

    def __call__(
        self,
        *,
        repo_id: str,
        revision: str,
        local_dir: Path,
        cache_dir: Path,
        token: bool,
        allow_patterns: tuple[str, ...],
        ignore_patterns: tuple[str, ...],
    ) -> str: ...


class ModelSnapshotError(ValueError):
    """Raised when snapshot metadata, storage, or downloaded content is invalid."""


@dataclass(frozen=True, slots=True)
class ModelSnapshot:
    """Pinned Hugging Face repository snapshot metadata."""

    snapshot_id: str
    runtime_profile: str
    purpose: str
    source_repository: str
    source_revision: str
    expected_size_bytes: int
    minimum_free_bytes: int
    license_id: str
    license_posture: str
    model_alias: str
    precision: str
    tier: ModelTier
    qualification: ModelQualification
    minimum_gpu_count: int
    minimum_gpu_memory_bytes: int
    recommended_ram_bytes: int
    recommended_disk_bytes: int
    maximum_context_window: int
    tool_call_parser: ToolCallParser
    tensor_parallel_size: int
    startup_seconds_min: int
    startup_seconds_max: int
    download_policy: str
    allow_patterns: tuple[str, ...]
    ignore_patterns: tuple[str, ...]
    validated_platforms: tuple[str, ...] = ()
    qualification_test: str | None = None
    qualification_date: str | None = None
    qualification_evidence: str | None = None
    context_window: int = DEFAULT_LOCAL_CONTEXT_WINDOW
    minimum_resource_envelope: str | None = None
    recommended_resource_envelope: str | None = None
    recommended_cpu_count: int = 8
    recommended: bool = False

    def validate(self) -> None:
        """Validate identity, source, and storage metadata."""
        if _SNAPSHOT_ID.fullmatch(self.snapshot_id) is None:
            raise ModelSnapshotError("snapshot_id must be a safe cache directory name")
        if not is_hugging_face_model_id(self.source_repository):
            raise ModelSnapshotError("source_repository must be a Hugging Face owner/repository id")
        if not is_resolved_revision(self.source_revision):
            raise ModelSnapshotError("source_revision must be an immutable commit revision")
        for name, value in (
            ("runtime_profile", self.runtime_profile),
            ("purpose", self.purpose),
            ("license_id", self.license_id),
            ("license_posture", self.license_posture),
            ("model_alias", self.model_alias),
            ("precision", self.precision),
            ("download_policy", self.download_policy),
        ):
            if not value:
                raise ModelSnapshotError(f"{name} must be a non-empty string")
        if self.expected_size_bytes <= 0 or self.minimum_free_bytes < self.expected_size_bytes:
            raise ModelSnapshotError("snapshot storage metadata is invalid")
        if self.recommended_disk_bytes < self.minimum_free_bytes:
            raise ModelSnapshotError("recommended_disk_bytes must cover minimum_free_bytes")
        if self.recommended_ram_bytes <= 0:
            raise ModelSnapshotError("recommended_ram_bytes must be positive")
        if self.recommended_cpu_count <= 0:
            raise ModelSnapshotError("recommended_cpu_count must be positive")
        if self.minimum_gpu_count <= 0 or self.minimum_gpu_memory_bytes <= 0:
            raise ModelSnapshotError("GPU resource metadata must be positive")
        if self.tensor_parallel_size < self.minimum_gpu_count:
            raise ModelSnapshotError("tensor_parallel_size must cover the minimum GPU count")
        if self.tier not in _MODEL_TIERS:
            raise ModelSnapshotError(f"unsupported model tier: {self.tier}")
        if self.qualification not in _MODEL_QUALIFICATIONS:
            raise ModelSnapshotError(f"unsupported model qualification: {self.qualification}")
        if self.tool_call_parser not in _TOOL_CALL_PARSERS:
            raise ModelSnapshotError(f"unsupported vLLM tool-call parser: {self.tool_call_parser}")
        if self.startup_seconds_min <= 0 or self.startup_seconds_max < self.startup_seconds_min:
            raise ModelSnapshotError("snapshot startup estimate is invalid")
        if not MINIMUM_LOCAL_CONTEXT_WINDOW <= self.context_window <= MAXIMUM_LOCAL_CONTEXT_WINDOW:
            raise ModelSnapshotError(
                f"context_window must be between 2048 and {MAXIMUM_LOCAL_CONTEXT_WINDOW} tokens"
            )
        if not self.context_window <= self.maximum_context_window <= MAXIMUM_LOCAL_CONTEXT_WINDOW:
            raise ModelSnapshotError(
                "maximum_context_window must cover the default context window and remain bounded"
            )
        if not self.allow_patterns:
            raise ModelSnapshotError("allow_patterns must select reviewed snapshot files")
        for name, patterns in (
            ("allow_patterns", self.allow_patterns),
            ("ignore_patterns", self.ignore_patterns),
        ):
            if len(patterns) != len(set(patterns)):
                raise ModelSnapshotError(f"{name} must not contain duplicates")
            if any(not _safe_pattern(pattern) for pattern in patterns):
                raise ModelSnapshotError(f"{name} contains an unsafe repository pattern")
        if len(self.validated_platforms) != len(set(self.validated_platforms)):
            raise ModelSnapshotError("validated_platforms must not contain duplicates")
        if any(platform not in _VALIDATED_PLATFORMS for platform in self.validated_platforms):
            raise ModelSnapshotError("qualification platforms contain an unsupported platform")
        if self.qualification == "qualified" and (
            not self.validated_platforms or self.qualification_test is None
        ):
            raise ModelSnapshotError(
                "qualified models require validated platforms and a qualification test"
            )
        if self.qualification == "unvalidated" and self.validated_platforms:
            raise ModelSnapshotError("unvalidated models cannot declare validated platforms")
        has_evidence = (
            self.qualification_date is not None and self.qualification_evidence is not None
        )
        if (self.qualification != "unvalidated") != has_evidence:
            raise ModelSnapshotError("qualified models require dated qualification evidence")
        if self.qualification != "qualified" and self.recommended:
            raise ModelSnapshotError("only qualified models can be recommended")

    def safe_dict(self) -> dict[str, object]:
        """Return non-secret catalog metadata."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ModelSnapshotCatalog:
    """Recommended multi-file snapshots keyed by stable id."""

    schema_version: str
    snapshots: tuple[ModelSnapshot, ...]

    def snapshot(self, snapshot_id: str) -> ModelSnapshot:
        """Return one snapshot from the repository recommendation catalog."""
        for snapshot in self.snapshots:
            if snapshot.snapshot_id == snapshot_id:
                return snapshot
        raise ModelSnapshotError(f"unknown model snapshot: {snapshot_id}")

    def safe_dict(self) -> dict[str, object]:
        """Return serializable catalog metadata."""
        return {
            "schema_version": self.schema_version,
            "snapshots": [snapshot.safe_dict() for snapshot in self.snapshots],
        }

    def recommend(
        self,
        *,
        platform_id: str,
        gpu_count: int,
        gpu_memory_bytes: int,
        maximum_tier: ModelTier,
        requested_gpus: int | None = None,
    ) -> ModelSnapshot | None:
        """Return the strongest qualified recommendation within reviewed resources."""
        maximum_rank = _MODEL_TIER_RANK[maximum_tier]
        candidates = [
            (index, snapshot)
            for index, snapshot in enumerate(self.snapshots)
            if snapshot.recommended
            and snapshot.qualification == "qualified"
            and platform_id in snapshot.validated_platforms
            and _MODEL_TIER_RANK[snapshot.tier] <= maximum_rank
            and snapshot.minimum_gpu_count <= gpu_count
            and snapshot.minimum_gpu_memory_bytes <= gpu_memory_bytes
            and (requested_gpus is None or snapshot.tensor_parallel_size == requested_gpus)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (
                _MODEL_TIER_RANK[item[1].tier],
                item[1].tensor_parallel_size,
                -item[0],
            ),
        )[1]

    def recommend_for_capacities(
        self,
        *,
        platform_id: str,
        capacities: tuple[tuple[int, int], ...],
        maximum_tier: ModelTier,
        requested_gpus: int | None = None,
    ) -> ModelSnapshot | None:
        """Return the strongest recommendation from distinct resource envelopes."""
        candidates = tuple(
            dict.fromkeys(
                recommendation
                for gpu_count, gpu_memory_bytes in capacities
                if (
                    recommendation := self.recommend(
                        platform_id=platform_id,
                        gpu_count=gpu_count,
                        gpu_memory_bytes=gpu_memory_bytes,
                        maximum_tier=maximum_tier,
                        requested_gpus=requested_gpus,
                    )
                )
                is not None
            )
        )
        if not candidates:
            return None
        positions = {snapshot.snapshot_id: index for index, snapshot in enumerate(self.snapshots)}
        return max(
            candidates,
            key=lambda snapshot: (
                _MODEL_TIER_RANK[snapshot.tier],
                snapshot.tensor_parallel_size,
                -positions[snapshot.snapshot_id],
            ),
        )


def load_model_snapshot_catalog(path: Path) -> ModelSnapshotCatalog:
    """Load recommended snapshot metadata from the repository catalog."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ModelSnapshotError(
            f"unable to load model snapshot catalog {path}: {error}"
        ) from error
    schema_version = _string(data, "schema_version")
    if schema_version != "heartwood.model-snapshot-catalog.v3":
        raise ModelSnapshotError(f"unsupported model snapshot catalog schema: {schema_version}")
    raw_snapshots = data.get("snapshots")
    if not isinstance(raw_snapshots, dict):
        raise ModelSnapshotError("model snapshot catalog must include a snapshots table")
    raw_policies = data.get("download_policies")
    if not isinstance(raw_policies, dict) or not raw_policies:
        raise ModelSnapshotError("model snapshot catalog must include download policies")
    policies: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for policy_id, policy in raw_policies.items():
        if not isinstance(policy_id, str) or not isinstance(policy, dict):
            raise ModelSnapshotError("download policy entries must be tables")
        policies[policy_id] = (
            _string_tuple(policy, "allow_patterns", required=True),
            _string_tuple(policy, "ignore_patterns"),
        )
    snapshots: list[ModelSnapshot] = []
    for snapshot_id, item in raw_snapshots.items():
        if not isinstance(snapshot_id, str) or not isinstance(item, dict):
            raise ModelSnapshotError("model snapshot entries must be tables")
        download_policy = _string(item, "download_policy")
        try:
            allow_patterns, ignore_patterns = policies[download_policy]
        except KeyError as error:
            raise ModelSnapshotError(
                f"unknown snapshot download policy: {download_policy}"
            ) from error
        snapshot = ModelSnapshot(
            snapshot_id=snapshot_id,
            runtime_profile=_string(item, "runtime_profile"),
            purpose=_string(item, "purpose"),
            source_repository=_string(item, "source_repository"),
            source_revision=_string(item, "source_revision"),
            expected_size_bytes=_positive_int(item, "expected_size_bytes"),
            minimum_free_bytes=_positive_int(item, "minimum_free_bytes"),
            license_id=_string(item, "license_id"),
            license_posture=_string(item, "license_posture"),
            model_alias=_string(item, "model_alias"),
            precision=_string(item, "precision"),
            tier=cast(ModelTier, _enum_string(item, "tier", _MODEL_TIERS)),
            qualification=cast(
                ModelQualification,
                _enum_string(item, "qualification", _MODEL_QUALIFICATIONS),
            ),
            minimum_gpu_count=_positive_int(item, "minimum_gpu_count"),
            minimum_gpu_memory_bytes=_positive_int(item, "minimum_gpu_memory_bytes"),
            recommended_cpu_count=_positive_int(item, "recommended_cpu_count"),
            recommended_ram_bytes=_positive_int(item, "recommended_ram_bytes"),
            recommended_disk_bytes=_positive_int(item, "recommended_disk_bytes"),
            maximum_context_window=_positive_int(item, "maximum_context_window"),
            tool_call_parser=cast(
                ToolCallParser,
                _enum_string(item, "tool_call_parser", _TOOL_CALL_PARSERS),
            ),
            tensor_parallel_size=_positive_int(item, "tensor_parallel_size"),
            startup_seconds_min=_positive_int(item, "startup_seconds_min"),
            startup_seconds_max=_positive_int(item, "startup_seconds_max"),
            download_policy=download_policy,
            allow_patterns=allow_patterns,
            ignore_patterns=ignore_patterns,
            validated_platforms=_string_tuple(item, "validated_platforms"),
            qualification_test=_optional_string(item, "qualification_test"),
            qualification_date=_optional_string(item, "qualification_date"),
            qualification_evidence=_optional_string(item, "qualification_evidence"),
            context_window=_positive_int(item, "context_window"),
            minimum_resource_envelope=_optional_string(item, "minimum_resource_envelope"),
            recommended_resource_envelope=_optional_string(item, "recommended_resource_envelope"),
            recommended=_optional_bool(item, "recommended", default=False),
        )
        snapshot.validate()
        snapshots.append(snapshot)
    return ModelSnapshotCatalog(schema_version, tuple(snapshots))


def download_model_snapshot(
    snapshot: ModelSnapshot,
    *,
    cache_dir: Path,
    downloader: SnapshotDownloader | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Download a pinned snapshot atomically and create an exact local manifest."""
    snapshot.validate()
    cache_dir = cache_dir.resolve()
    destination = (cache_dir / snapshot.snapshot_id).resolve()
    if cache_dir != destination and cache_dir not in destination.parents:
        raise ModelSnapshotError("model snapshot path escapes configured cache directory")
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(cache_dir / f".{snapshot.snapshot_id}.lock", mode=0o600):
        if destination.exists():
            try:
                verify_model_snapshot(destination)
                _verify_source_record(destination, snapshot)
            except (OSError, UnicodeError, ValueError) as error:
                raise ModelSnapshotError(
                    f"existing model snapshot is incomplete or modified: {destination}: {error}"
                ) from error
            if progress_callback is not None:
                progress_callback(snapshot.expected_size_bytes, snapshot.expected_size_bytes)
            return destination
        available = shutil.disk_usage(cache_dir).free
        if available < snapshot.minimum_free_bytes:
            required_gib = snapshot.minimum_free_bytes / (1024**3)
            available_gib = available / (1024**3)
            raise ModelSnapshotError(
                f"snapshot requires at least {required_gib:.0f} GiB free; "
                f"{available_gib:.1f} GiB is available under {cache_dir}"
            )
        if downloader is None:
            downloader = cast(
                SnapshotDownloader,
                import_module("huggingface_hub").snapshot_download,
            )
        staging = Path(tempfile.mkdtemp(prefix=f".{snapshot.snapshot_id}.", dir=cache_dir))
        progress_stop = threading.Event()
        progress_thread: threading.Thread | None = None
        if progress_callback is not None:
            progress_callback(0, snapshot.expected_size_bytes)
            progress_thread = threading.Thread(
                target=_monitor_download_progress,
                args=(
                    staging,
                    snapshot.expected_size_bytes,
                    progress_callback,
                    progress_stop,
                ),
                daemon=True,
                name=f"heartwood-snapshot-progress-{snapshot.snapshot_id}",
            )
            progress_thread.start()
        try:
            try:
                downloader(
                    repo_id=snapshot.source_repository,
                    revision=snapshot.source_revision,
                    local_dir=staging,
                    cache_dir=staging / ".cache" / "huggingface",
                    token=False,
                    allow_patterns=snapshot.allow_patterns,
                    ignore_patterns=snapshot.ignore_patterns,
                )
            finally:
                progress_stop.set()
                if progress_thread is not None:
                    progress_thread.join()
                if progress_callback is not None:
                    progress_callback(
                        min(_directory_size(staging), snapshot.expected_size_bytes),
                        snapshot.expected_size_bytes,
                    )
            shutil.rmtree(staging / ".cache", ignore_errors=True)
            _verify_download_size(staging, snapshot)
            source_record = {
                "schema_version": "heartwood.model-snapshot-source.v2",
                "snapshot_id": snapshot.snapshot_id,
                "source_repository": snapshot.source_repository,
                "source_revision": snapshot.source_revision,
                "download_policy": snapshot.download_policy,
                "allow_patterns": list(snapshot.allow_patterns),
                "ignore_patterns": list(snapshot.ignore_patterns),
            }
            (staging / "HEARTWOOD-SOURCE.json").write_text(
                json.dumps(source_record, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            write_model_snapshot_manifest(staging)
            verify_model_snapshot(staging)
            _verify_source_record(staging, snapshot)
            staging.replace(destination)
            if progress_callback is not None:
                progress_callback(snapshot.expected_size_bytes, snapshot.expected_size_bytes)
        finally:
            progress_stop.set()
            if progress_thread is not None and progress_thread.is_alive():
                progress_thread.join()
            shutil.rmtree(staging, ignore_errors=True)
        return destination


def _monitor_download_progress(
    root: Path,
    total: int,
    callback: ProgressCallback,
    stop: threading.Event,
) -> None:
    while not stop.wait(0.25):
        callback(min(_directory_size(root), total), total)


def _directory_size(root: Path) -> int:
    try:
        return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    except OSError:
        return 0


def verify_model_snapshot(root: Path) -> None:
    """Reject unlisted, missing, linked, duplicated, or modified snapshot files."""
    manifest = root / "SHA256SUMS"
    if root.is_symlink() or not root.is_dir() or not manifest.is_file() or manifest.is_symlink():
        raise ValueError("model root must contain a regular SHA256SUMS manifest")
    expected: dict[str, str] = {}
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        match = _ENTRY.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid SHA256SUMS entry on line {line_number}")
        digest, name = match.groups()
        manifest_relative = PurePosixPath(name)
        if (
            manifest_relative.is_absolute()
            or ".." in manifest_relative.parts
            or name in {"", "SHA256SUMS"}
        ):
            raise ValueError(f"unsafe SHA256SUMS path on line {line_number}")
        normalized = manifest_relative.as_posix()
        if normalized in expected:
            raise ValueError(f"duplicate SHA256SUMS path: {normalized}")
        expected[normalized] = digest.lower()

    actual: set[str] = set()
    for path in root.rglob("*"):
        snapshot_relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"model snapshot contains a symbolic link: {snapshot_relative}")
        if path.is_file() and snapshot_relative != "SHA256SUMS":
            actual.add(snapshot_relative)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        unlisted = sorted(actual - set(expected))
        detail = "; ".join(
            item
            for item in (
                f"missing: {', '.join(missing)}" if missing else "",
                f"unlisted: {', '.join(unlisted)}" if unlisted else "",
            )
            if item
        )
        raise ValueError(f"model snapshot does not match SHA256SUMS coverage ({detail})")

    for relative_name, expected_digest in expected.items():
        hasher = hashlib.sha256()
        descriptor = os.open(root / relative_name, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as file:
            while chunk := file.read(1024 * 1024):
                hasher.update(chunk)
        if hasher.hexdigest() != expected_digest:
            raise ValueError(f"SHA-256 mismatch: {relative_name}")


def write_model_snapshot_manifest(root: Path) -> None:
    """Write an exact SHA-256 coverage manifest for a prepared snapshot."""
    entries: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == "SHA256SUMS" or path.is_symlink():
            continue
        hasher = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                hasher.update(chunk)
        entries.append(f"{hasher.hexdigest()}  {relative}")
    (root / "SHA256SUMS").write_text("\n".join(entries) + "\n", encoding="utf-8")


def _verify_download_size(root: Path, snapshot: ModelSnapshot) -> None:
    actual = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    lower = int(snapshot.expected_size_bytes * (1 - _SIZE_TOLERANCE))
    upper = int(snapshot.expected_size_bytes * (1 + _SIZE_TOLERANCE))
    if not lower <= actual <= upper:
        raise ModelSnapshotError(
            f"downloaded snapshot size {actual} bytes is outside the reviewed range "
            f"{lower}-{upper} bytes"
        )


def _verify_source_record(root: Path, snapshot: ModelSnapshot) -> None:
    source_path = root / "HEARTWOOD-SOURCE.json"
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelSnapshotError("model snapshot source record is unavailable") from error
    expected = {
        "schema_version": "heartwood.model-snapshot-source.v2",
        "snapshot_id": snapshot.snapshot_id,
        "source_repository": snapshot.source_repository,
        "source_revision": snapshot.source_revision,
        "download_policy": snapshot.download_policy,
        "allow_patterns": list(snapshot.allow_patterns),
        "ignore_patterns": list(snapshot.ignore_patterns),
    }
    if source != expected:
        raise ModelSnapshotError("model snapshot source record does not match the reviewed source")


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ModelSnapshotError(f"{key} must be a non-empty string")
    return value


def _positive_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ModelSnapshotError(f"{key} must be a positive integer")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ModelSnapshotError(f"{key} must be a non-empty string when provided")
    return value


def _optional_bool(data: dict[str, Any], key: str, *, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ModelSnapshotError(f"{key} must be a boolean")
    return value


def _enum_string(data: dict[str, Any], key: str, allowed: set[str]) -> str:
    value = _string(data, key)
    if value not in allowed:
        raise ModelSnapshotError(f"unsupported {key}: {value}")
    return value


def _string_tuple(
    data: dict[str, Any],
    key: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ModelSnapshotError(f"{key} must be an array of non-empty strings")
    if required and not value:
        raise ModelSnapshotError(f"{key} must not be empty")
    return tuple(value)


def _safe_pattern(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        _SAFE_PATTERN.fullmatch(value) is not None
        and not path.is_absolute()
        and ".." not in path.parts
    )
