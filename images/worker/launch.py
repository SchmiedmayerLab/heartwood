# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Start the native agent server with a fixed worker environment."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

_SUPERVISOR_CA = "/run/openshell-supervisor-ca/material"
_TLS_ENVIRONMENT = {
    "SSL_CERT_FILE": f"{_SUPERVISOR_CA}/ca-bundle.crt",
    "REQUESTS_CA_BUNDLE": f"{_SUPERVISOR_CA}/ca-bundle.crt",
    "CURL_CA_BUNDLE": f"{_SUPERVISOR_CA}/ca-bundle.crt",
    "GIT_SSL_CAINFO": f"{_SUPERVISOR_CA}/ca-bundle.crt",
    "NODE_EXTRA_CA_CERTS": f"{_SUPERVISOR_CA}/ca.crt",
    "DENO_CERT": f"{_SUPERVISOR_CA}/ca.crt",
}


def worker_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Admit scoped credentials and the exact supervisor-owned public trust paths."""
    key = source.get("SESSION_API_KEY", "")
    if re.fullmatch(r"[A-Za-z0-9_-]{43,256}", key) is None:
        raise ValueError("worker launch requires a URL-safe attempt key of at least 43 characters")
    environment = {
        "PATH": "/opt/heartwood/.venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/heartwood",
        "USER": "heartwood",
        "LOGNAME": "heartwood",
        "SHELL": "/bin/bash",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "OPENHANDS_SUPPRESS_BANNER": "1",
        "LOG_LEVEL": "ERROR",
        "OH_TELEMETRY_CONSENT": "denied",
        "OH_TELEMETRY_CONSENT_MODE": "override",
        "OPENHANDS_AGENT_SERVER_CONFIG_PATH": "/etc/heartwood/worker.json",
        "SESSION_API_KEY": key,
    }
    # The sandbox supplies these public CA files outside worker-writable paths.
    # Equality admits its maintained TLS contract without trusting arbitrary overrides.
    environment.update(
        (name, expected)
        for name, expected in _TLS_ENVIRONMENT.items()
        if source.get(name) == expected
    )
    placeholder = source.get("HEARTWOOD_INFERENCE_PLACEHOLDER")
    if placeholder is not None:
        if not placeholder or len(placeholder) > 4096 or any(c in placeholder for c in "\0\r\n"):
            raise ValueError("worker inference placeholder is invalid")
        environment["HEARTWOOD_INFERENCE_PLACEHOLDER"] = placeholder
    return environment


def main() -> None:
    """Replace the bootstrap process, preserving only the container's standard streams."""
    if sys.platform != "linux" or os.geteuid() == 0:
        raise SystemExit("worker image requires Linux and a non-root identity")
    try:
        environment = worker_environment(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    os.umask(0o077)
    os.chdir("/workspace")
    # Enumerating procfs handles descriptors above a subsequently lowered rlimit.
    # The directory descriptor used by iterdir is closed before the close loop.
    descriptors = [int(path.name) for path in Path("/proc/self/fd").iterdir()]
    for descriptor in descriptors:
        if descriptor > 2:
            os.closerange(descriptor, descriptor + 1)
    os.execve(
        sys.executable,
        [
            sys.executable,
            "-I",
            "-B",
            "-u",
            "-m",
            "openhands.agent_server",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        environment,
    )


if __name__ == "__main__":
    main()
