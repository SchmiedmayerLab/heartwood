# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Select deployment-owned environment settings for local agent terminals."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# Exact names avoid forwarding a newly introduced credential through a prefix match.
TERMINAL_ENVIRONMENT_NAMES = frozenset(
    [
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "PYTHONPATH",
        "CUDA_HOME",
        "CUDA_PATH",
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
        "NVIDIA_DRIVER_CAPABILITIES",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "SLURM_JOB_ID",
        "SLURM_JOB_NAME",
        "SLURM_SUBMIT_DIR",
        "SLURM_CPUS_PER_TASK",
        "SLURM_NTASKS",
        "SLURM_PROCID",
        "SLURM_LOCALID",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "NODE_EXTRA_CA_CERTS",
    ]
)
TERMINAL_ENVIRONMENT_EXTENSION = "HEARTWOOD_TERMINAL_ENV_ALLOWLIST"


def terminal_environment(
    source: Mapping[str, str], *, excluded: Sequence[str] = ()
) -> dict[str, str]:
    """Return only explicitly admitted settings, never configured credentials."""
    additional = source.get(TERMINAL_ENVIRONMENT_EXTENSION, "")
    names = {name.strip() for name in additional.split(",") if name.strip()}
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in names):
        raise ValueError(
            f"{TERMINAL_ENVIRONMENT_EXTENSION} requires comma-separated variable names"
        )
    allowed = (TERMINAL_ENVIRONMENT_NAMES | names) - set(excluded)
    return {name: source[name] for name in sorted(allowed) if name in source}
