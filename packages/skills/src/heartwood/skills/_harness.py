# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Deterministic test harness for local skills."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from heartwood.skills._verification import LocalSkillVerifier, SkillManifest, SkillVerification


@dataclass(frozen=True, slots=True)
class SkillTestHarness:
    """Verify and run local skill entrypoints in tests."""

    root: Path

    def verify_all(self) -> tuple[SkillVerification, ...]:
        """Verify every direct child skill under the harness root."""
        verifier = LocalSkillVerifier(self.root)
        return tuple(verifier.verify(path) for path in sorted(self.root.iterdir()) if path.is_dir())

    def run(
        self,
        manifest: SkillManifest,
        *args: str,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a skill entrypoint with explicit arguments."""
        return subprocess.run(
            [sys.executable, str(manifest.entrypoint), *args],
            cwd=cwd,
            check=True,
            text=True,
            capture_output=True,
        )
