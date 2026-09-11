# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read the current Python process through standard interpreter and package metadata."""

import platform
import subprocess
import sys
from importlib.metadata import distributions

from heartwood.schemas.python_environment import PythonEnvironmentSnapshot


def observe_python_environment() -> PythonEnvironmentSnapshot:
    """Describe this process, never infer the environment of an external interpreter."""
    return PythonEnvironmentSnapshot(
        implementation=platform.python_implementation(),
        python=platform.python_version(),
        system=platform.system(),
        machine=platform.machine(),
        packages=tuple(sorted((item.metadata["Name"], item.version) for item in distributions())),
    )


def inspect_verification_environment() -> PythonEnvironmentSnapshot:
    """Inspect the exact isolated Python used by verification, without running user code."""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-m", "heartwood.gateway._environment_probe", "--stdout"],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return PythonEnvironmentSnapshot.model_validate_json(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("The verification Python environment could not be inspected") from None
