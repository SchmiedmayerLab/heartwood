# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Requests with curl from a probe pod in heartwood-sessions.

Tokens, headers and forms reach curl through files in the probe pod, written over
standard input and checked, and never on a command line.
"""

from __future__ import annotations

import secrets
import warnings

from kube import Cluster

SESSIONS = "heartwood-sessions"


def write_private(cluster: Cluster, pod: str, text: str) -> str:
    """Write ``text`` to a new file that only the probe reads, and return its path.

    Data sent to ``kubectl exec`` on standard input can arrive incomplete, so the
    write reports the size it wrote and is repeated until the size matches. Each
    incomplete write is reported as a warning, so a passing run shows whether a
    repetition was needed.
    """
    path = f"/scratch/{secrets.token_hex(8)}.data"
    expected = len(text.encode())
    for attempt in range(1, 6):
        written = cluster.kubectl(
            "exec",
            "--stdin",
            f"--namespace={SESSIONS}",
            pod,
            "--",
            "sh",
            "-c",
            f"umask 077 && cat > {path} && wc -c < {path}",
            text_input=text,
        )
        if written.returncode == 0 and written.stdout.strip() == str(expected):
            return path
        warnings.warn(
            f"write {attempt} to the probe was incomplete: exit {written.returncode}, "
            f"{written.stdout.strip() or 'no'} of {expected} bytes",
            stacklevel=2,
        )
    raise AssertionError(f"the probe did not receive {expected} bytes")


def curl(cluster: Cluster, pod: str, *arguments: str, text_input: str = "") -> str:
    """Run curl in the probe pod and return its output.

    Arguments that read standard input (``@-``) read ``text_input`` from a file that
    is written and checked first.
    """
    path = write_private(cluster, pod, text_input) if text_input else None
    if path is not None:
        arguments = tuple(f"@{path}" if argument == "@-" else argument for argument in arguments)
    try:
        result = cluster.kubectl(
            "exec",
            f"--namespace={SESSIONS}",
            pod,
            "--",
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "60",
            *arguments,
        )
    finally:
        if path is not None:
            cluster.kubectl("exec", f"--namespace={SESSIONS}", pod, "--", "rm", "-f", path)
    assert result.returncode == 0, result.stderr
    return result.stdout
