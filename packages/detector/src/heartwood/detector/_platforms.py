# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic, propose-not-commit platform detection.

The platform probe inspects environment markers only. It never calls a model,
reads participant-level data, or loads a skill; it returns a *proposal* — the
platform, a confidence, and the evidence behind it — for a human to confirm.

See ``design/04-skills.md`` (auto-detection) and ``design/03-architecture.md``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Platform(StrEnum):
    """A supported execution platform, identified by its adapter id."""

    TERRA = "terra"
    DNANEXUS = "dnanexus"
    SEVEN_BRIDGES = "seven-bridges"
    GENERIC = "generic"


@dataclass(frozen=True, slots=True)
class PlatformDetection:
    """A proposed platform with the confidence and evidence behind it.

    This is a proposal only: producing it neither loads nor runs anything.
    """

    platform: Platform
    confidence: float
    evidence: tuple[str, ...]


# Environment markers per managed platform: (exact keys, key prefixes). Generic
# is the fallback when none match. Kept deliberately small and auditable.
_MARKERS: dict[Platform, tuple[tuple[str, ...], tuple[str, ...]]] = {
    Platform.TERRA: (("GOOGLE_PROJECT",), ("WORKSPACE_",)),
    Platform.DNANEXUS: ((), ("DX_",)),
    Platform.SEVEN_BRIDGES: ((), ("SB_",)),
}


def _matched_markers(
    env: Mapping[str, str], exact: tuple[str, ...], prefixes: tuple[str, ...]
) -> list[str]:
    """Return the environment keys that match a platform's exact keys or prefixes."""
    matched = [key for key in exact if key in env]
    # ``str.startswith(())`` is False, so an empty prefix tuple matches nothing.
    matched += sorted(k for k in env if k.startswith(prefixes) and k not in matched)
    return matched


def detect_platform(env: Mapping[str, str] | None = None) -> PlatformDetection:
    """Propose the platform from environment markers.

    Args:
        env: The environment mapping to inspect. Defaults to ``os.environ``.

    Returns:
        A :class:`PlatformDetection` proposal. This function has no side effects
        and never loads or runs anything.
    """
    if env is None:
        env = os.environ

    matches: dict[Platform, list[str]] = {}
    for platform, (exact, prefixes) in _MARKERS.items():
        found = _matched_markers(env, exact, prefixes)
        if found:
            matches[platform] = found

    if not matches:
        return PlatformDetection(
            platform=Platform.GENERIC,
            confidence=1.0,
            evidence=("no managed-platform environment markers detected",),
        )

    if len(matches) == 1:
        platform, found = next(iter(matches.items()))
        confidence = min(0.99, 0.9 + 0.03 * (len(found) - 1))
        evidence = tuple(f"found environment marker {marker}" for marker in found)
        return PlatformDetection(platform=platform, confidence=confidence, evidence=evidence)

    # Ambiguous: markers for more than one platform are present. Choose the
    # strongest (deterministic tie-break by id) and flag the low confidence.
    platform, found = max(matches.items(), key=lambda item: (len(item[1]), item[0].value))
    others = sorted(candidate.value for candidate in matches if candidate is not platform)
    evidence = (
        *(f"found environment marker {marker}" for marker in found),
        f"ambiguous: markers for {', '.join(others)} also present",
    )
    return PlatformDetection(platform=platform, confidence=0.5, evidence=evidence)
