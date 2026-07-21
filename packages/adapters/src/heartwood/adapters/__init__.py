# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Adapter protocols and conformance helpers for Heartwood."""

from __future__ import annotations

from heartwood.adapters._protocols import (
    AdapterDetection,
    DatasetFingerprint,
    DataSourceAdapter,
    PlatformAdapter,
    PlatformCapabilities,
    RegistryAdapter,
    RegistryVerification,
    SkillReference,
)
from heartwood.adapters.conformance import (
    assert_data_source_adapter_conforms,
    assert_platform_adapter_conforms,
    assert_registry_adapter_conforms,
)

__all__ = [
    "AdapterDetection",
    "DataSourceAdapter",
    "DatasetFingerprint",
    "PlatformAdapter",
    "PlatformCapabilities",
    "RegistryAdapter",
    "RegistryVerification",
    "SkillReference",
    "__version__",
    "assert_data_source_adapter_conforms",
    "assert_platform_adapter_conforms",
    "assert_registry_adapter_conforms",
]

__version__ = "0.2.0-beta.5"
