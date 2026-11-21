# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Fixtures of the cluster tests.

The tests need an installed chart. They run only with --cluster-context, which names
the kubectl context of that cluster. --staging-image names one of the sessions.stagingImages
that the chart was installed with.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from kube import Cluster

_VALUES = Path(__file__).resolve().parents[2] / "chart" / "heartwood-platform" / "values-kind.yaml"
# The image of the pods that the tests run and exec into, with its /agnhost commands.
_PROBE_IMAGE = (
    "registry.k8s.io/e2e-test-images/agnhost:2.53"
    "@sha256:99c6b4bb4a1e1df3f0b3752168c89358794d02258ebebc26bf21c29399011a85"
)


def _kind_staging_image() -> str:
    values = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))
    image = values["sessions"]["stagingImages"][0]
    assert isinstance(image, str)
    return image


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--cluster-context", required=True, help="kubectl context of the cluster")
    parser.addoption(
        "--staging-image",
        default=_kind_staging_image(),
        help="an image of sessions.stagingImages of the installed chart (default: values-kind)",
    )


@pytest.fixture(scope="session")
def cluster(request: pytest.FixtureRequest) -> Cluster:
    return Cluster(request.config.getoption("--cluster-context"))


@pytest.fixture(scope="session")
def staging_image(request: pytest.FixtureRequest) -> str:
    """The image that the installed staging policy admits."""
    image = request.config.getoption("--staging-image")
    assert isinstance(image, str)
    return image


@pytest.fixture(scope="session")
def probe_image() -> str:
    """The image of the pods that the tests run."""
    return _PROBE_IMAGE
