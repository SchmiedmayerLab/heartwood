# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Fixtures of the cluster tests.

The tests need an installed chart. They run only with --cluster-context, which names
the kubectl context of that cluster.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from kube import Cluster

_VALUES = Path(__file__).resolve().parents[2] / "chart" / "heartwood-platform" / "values-kind.yaml"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--cluster-context", required=True, help="kubectl context of the cluster")


@pytest.fixture(scope="session")
def cluster(request: pytest.FixtureRequest) -> Cluster:
    return Cluster(request.config.getoption("--cluster-context"))


@pytest.fixture(scope="session")
def staging_image() -> str:
    values = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))
    image = values["sessions"]["stagingImage"]
    assert isinstance(image, str)
    return image
