# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read the current Python process through standard interpreter and package metadata."""

import os
import subprocess
import sys
from pathlib import Path

from heartwood.gateway import _python_metadata
from heartwood.schemas.python_environment import PythonEnvironmentSnapshot


def observe_python_environment() -> PythonEnvironmentSnapshot:
    """Describe this process, never infer the environment of an external interpreter."""
    return PythonEnvironmentSnapshot.model_validate(_python_metadata.python_metadata())


def inspect_verification_environment(
    python: str | Path = sys.executable,
) -> PythonEnvironmentSnapshot:
    """Inspect the exact isolated Python used by verification, without running user code."""
    try:
        result = subprocess.run(
            [str(python), "-I", str(Path(_python_metadata.__file__))],
            capture_output=True,
            check=True,
            timeout=30,
            env={"PATH": os.defpath},
        )
        return PythonEnvironmentSnapshot.model_validate_json(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("The verification Python environment could not be inspected") from None
