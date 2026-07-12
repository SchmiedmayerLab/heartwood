# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Unit tests for deterministic platform detection against synthetic env fixtures."""

from __future__ import annotations

import pytest

from heartwood.detector import (
    Platform,
    PlatformDetection,
    detect_platform,
    platform_detection_evidence,
)


def test_generic_when_no_markers() -> None:
    detection = detect_platform({})
    assert detection.platform is Platform.GENERIC
    assert detection.confidence == 1.0
    assert detection.evidence


def test_carina_from_explicit_platform_evidence() -> None:
    detection = detect_platform({"HEARTWOOD_PLATFORM": "carina"})
    assert detection.platform is Platform.CARINA
    assert "HEARTWOOD_PLATFORM=carina" in detection.evidence[0]


def test_carina_from_cluster_name() -> None:
    detection = detect_platform({"SLURM_CLUSTER_NAME": "carina2"})
    assert detection.platform is Platform.CARINA


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"GOOGLE_PROJECT": "aou-synthetic"}, Platform.TERRA),
        ({"WORKSPACE_NAMESPACE": "ns", "WORKSPACE_BUCKET": "gs://b"}, Platform.TERRA),
        ({"DX_JOB_ID": "job-xxxx"}, Platform.DNANEXUS),
        ({"SB_API_ENDPOINT": "https://api.sbgenomics.com"}, Platform.SEVEN_BRIDGES),
    ],
)
def test_managed_platform_detected(env: dict[str, str], expected: Platform) -> None:
    detection = detect_platform(env)
    assert detection.platform is expected
    assert 0.9 <= detection.confidence <= 0.99
    assert detection.evidence


def test_more_markers_increase_confidence() -> None:
    one = detect_platform({"WORKSPACE_NAMESPACE": "ns"})
    many = detect_platform(
        {"WORKSPACE_NAMESPACE": "ns", "WORKSPACE_BUCKET": "b", "GOOGLE_PROJECT": "p"}
    )
    assert many.confidence > one.confidence


def test_ambiguous_markers_lower_confidence() -> None:
    detection = detect_platform({"GOOGLE_PROJECT": "p", "DX_JOB_ID": "j"})
    assert detection.platform in {Platform.TERRA, Platform.DNANEXUS}
    assert detection.confidence == 0.5
    assert any("ambiguous" in item for item in detection.evidence)


def test_default_reads_process_environment() -> None:
    detection = detect_platform()
    assert isinstance(detection, PlatformDetection)
    assert isinstance(detection.platform, Platform)


def test_platform_detection_exports_detector_evidence_schema() -> None:
    detection = detect_platform({})
    evidence = platform_detection_evidence(detection, detection_id="detect-synthetic")
    assert evidence.schema_version == "heartwood.detector-evidence.v1"
    assert evidence.detector_kind == "platform"
    assert evidence.candidate_id == "generic"
