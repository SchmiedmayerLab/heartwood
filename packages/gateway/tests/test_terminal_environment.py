# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Local deployment settings reach terminals without unrelated credentials."""

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from heartwood.gateway._terminal_environment import terminal_environment


@pytest.mark.parametrize(
    "platform_setting", ["VIRTUAL_ENV", "CUDA_VISIBLE_DEVICES", "SLURM_JOB_ID"]
)
def test_research_environment_reaches_child_without_tokens(platform_setting: str) -> None:
    source = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": "/research/source",
        platform_setting: "research-setting",
        "AWS_SECRET_ACCESS_KEY": "synthetic",
        "GITHUB_TOKEN": "synthetic",
        "HF_TOKEN": "synthetic",
        "JUPYTERHUB_API_TOKEN": "synthetic",
        "JPY_API_TOKEN": "synthetic",
        "GOOGLE_APPLICATION_CREDENTIALS": "/private/synthetic.json",
        "NEW_PROVIDER_SECRET": "synthetic",
    }
    child = subprocess.run(
        ["/usr/bin/env"],
        env=terminal_environment(source),
        capture_output=True,
        text=True,
        check=True,
    )
    names = {line.split("=", 1)[0] for line in child.stdout.splitlines()}
    assert names == {"PATH", "PYTHONPATH", platform_setting}
    assert f"{platform_setting}=research-setting" in child.stdout


def test_deployment_can_explicitly_extend_environment() -> None:
    assert terminal_environment(
        {
            "HEARTWOOD_TERMINAL_ENV_ALLOWLIST": " RESEARCH_DATA_ROOT,RESEARCH_THREADS ",
            "RESEARCH_DATA_ROOT": "/research/data",
            "RESEARCH_THREADS": "2",
            "UNLISTED_SECRET": "synthetic",
        }
    ) == {"RESEARCH_DATA_ROOT": "/research/data", "RESEARCH_THREADS": "2"}


def test_invalid_environment_extension_is_rejected() -> None:
    with pytest.raises(ValueError, match="comma-separated variable names"):
        terminal_environment({"HEARTWOOD_TERMINAL_ENV_ALLOWLIST": "SECRET=synthetic"})


def test_runtime_script_path_reaches_terminal(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime with spaces"
    runtime_root.mkdir()
    (runtime_root / "analysis.py").write_text(
        "from pathlib import Path\nPath('summary.json').write_text('{}')\n"
    )
    child = subprocess.run(
        ["/bin/sh", "-c", f'{shlex.quote(sys.executable)} "$HEARTWOOD_RUNTIME_ROOT"/analysis.py'],
        cwd=tmp_path,
        env=terminal_environment(
            {
                "HEARTWOOD_RUNTIME_ROOT": str(runtime_root),
                "HEARTWOOD_UNUSED_MODEL_API_KEY": "synthetic-unused-model-key",
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert (tmp_path / "summary.json").read_text() == "{}"
