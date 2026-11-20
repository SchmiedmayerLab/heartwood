# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The model route provider in the OpenShell gateway of a session.

The control service registers the session's OpenShell gateway with the session's client
certificate in a temporary directory, runs the pinned OpenShell command-line tool, and
removes the directory. The provider holds the session's virtual key; a sandbox that
attaches it sees a placeholder, which the supervisor replaces with the key only in
requests to the endpoint of the provider profile. The key reaches the tool through its
environment, never through its arguments.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_GATEWAY = "session"
_TIMEOUT_SECONDS = 60


class ProviderError(Exception):
    """The OpenShell gateway did not complete a provider operation."""


@dataclass(frozen=True)
class ProviderRoute:
    """The provider that a session's workers attach for the model route."""

    # The provider's name, which the worker manifest names, and its profile identifier.
    name: str
    workspace: str
    # The environment variable through which a worker sees the placeholder of the key.
    placeholder_env: str
    host: str
    port: int
    path: str
    # Programs in the worker that may send the key.
    binaries: tuple[str, ...]

    def profile(self) -> dict[str, Any]:
        """Return the provider profile: one credential, valid only at the route's endpoint."""
        return {
            "id": self.name,
            "display_name": "Platform model route",
            "category": "inference",
            "inference_capable": True,
            "credentials": [
                {
                    "name": "api_key",
                    "env_vars": [self.placeholder_env],
                    "required": True,
                    "auth_style": "bearer",
                    "header_name": "authorization",
                }
            ],
            "endpoints": [
                {
                    "host": self.host,
                    "port": self.port,
                    "path": self.path,
                    "protocol": "rest",
                    "enforcement": "enforce",
                    "rules": [{"allow": {"method": "POST", "path": self.path}}],
                }
            ],
            "binaries": list(self.binaries),
        }


Runner = Callable[[Sequence[str], dict[str, str]], subprocess.CompletedProcess[str]]


def _run(arguments: Sequence[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        env=environment,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )


class OpenShellProviders:
    """Creates and updates the model route provider in session OpenShell gateways."""

    def __init__(
        self,
        cli: Path,
        route: ProviderRoute,
        scratch: Path,
        run: Runner = _run,
    ) -> None:
        """Run ``cli`` with registrations in temporary directories under ``scratch``."""
        self._cli = cli
        self._route = route
        self._scratch = scratch
        self._runner = run

    def _session(
        self, directory: Path, endpoint: str, client: dict[str, str]
    ) -> Callable[..., subprocess.CompletedProcess[str]]:
        mtls = directory / "openshell" / "gateways" / _GATEWAY / "mtls"
        mtls.mkdir(parents=True, mode=0o700)
        for name in ("ca.crt", "tls.crt", "tls.key"):
            path = mtls / name
            path.write_text(client[name], encoding="utf-8")
            path.chmod(0o600)
        base = {"PATH": os.defpath, "HOME": str(directory), "XDG_CONFIG_HOME": str(directory)}

        def run(
            *arguments: str, workspace: bool = True, **environment: str
        ) -> subprocess.CompletedProcess[str]:
            scope = ["--gateway", _GATEWAY]
            if workspace:
                scope += ["--workspace", self._route.workspace]
            return self._runner([str(self._cli), *arguments, *scope], {**base, **environment})

        added = self._runner(
            [str(self._cli), "gateway", "add", endpoint, "--name", _GATEWAY, "--local"], base
        )
        _require(added, "register the session's OpenShell gateway")
        return run

    def apply(self, endpoint: str, client: dict[str, str], key: str, *, replace: bool) -> None:
        """Give the session's workers the model route with ``key``.

        The workspace, the profile and the provider are created when missing. An existing
        provider keeps its credential unless ``replace`` is set, which is how a rotated key
        reaches the session.
        """
        with TemporaryDirectory(dir=self._scratch, prefix="openshell-") as name:
            directory = Path(name)
            run = self._session(directory, endpoint, client)
            route = self._route
            if run("workspace", "get", route.workspace, workspace=False).returncode != 0:
                _require(
                    run("workspace", "create", "--name", route.workspace, workspace=False),
                    "create the workspace",
                )
            if run("profile", "describe", route.name).returncode != 0:
                profile = directory / "profile.json"
                profile.write_text(json.dumps(route.profile()), encoding="utf-8")
                _require(
                    run("profile", "import", "--file", str(profile)), "import the provider profile"
                )
            credential = {route.placeholder_env: key}
            if run("provider", "get", route.name).returncode != 0:
                created = run(
                    "provider",
                    "create",
                    "--name",
                    route.name,
                    "--type",
                    route.name,
                    "--credential",
                    route.placeholder_env,
                    **credential,
                )
                _require(created, "create the provider")
            elif replace:
                updated = run(
                    "provider",
                    "update",
                    route.name,
                    "--credential",
                    route.placeholder_env,
                    **credential,
                )
                _require(updated, "update the provider")

    def exists(self, endpoint: str, client: dict[str, str]) -> bool:
        """Return whether the session's OpenShell gateway holds the provider."""
        with TemporaryDirectory(dir=self._scratch, prefix="openshell-") as name:
            run = self._session(Path(name), endpoint, client)
            return run("provider", "get", self._route.name).returncode == 0


def _require(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode != 0:
        # The tool reports errors without credentials; its last line names the cause.
        lines = result.stderr.strip().splitlines()
        cause = lines[-1][:300] if lines else f"exit status {result.returncode}"
        raise ProviderError(f"could not {action}: {cause}")
