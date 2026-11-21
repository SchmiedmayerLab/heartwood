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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_GATEWAY = "session"
_TIMEOUT_SECONDS = 60
# Right after a new session's OpenShell gateway is ready, and while a restarted one starts,
# the tool can fail to connect to it for a few seconds, likely until its Service and
# network policy are in effect. A request that cannot connect is repeated with growing
# pauses until this many seconds have passed since the first attempt.
_CONNECT_SECONDS = 60.0
_CONNECT_FIRST_PAUSE_SECONDS = 0.5
_CONNECT_LONGEST_PAUSE_SECONDS = 8.0


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


# The arguments, the environment and the seconds that the tool may run.
Runner = Callable[[Sequence[str], dict[str, str], float], subprocess.CompletedProcess[str]]


def _run(
    arguments: Sequence[str], environment: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
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
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Run ``cli`` with registrations in temporary directories under ``scratch``."""
        self._cli = cli
        self._route = route
        self._scratch = scratch
        self._runner = run
        self._sleep = sleep
        self._clock = clock

    def _call(
        self, arguments: Sequence[str], environment: dict[str, str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(arguments, environment, timeout)
        except subprocess.TimeoutExpired:
            raise ProviderError(
                f"the OpenShell tool did not finish within {timeout:.0f} seconds"
            ) from None

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
            *arguments: str,
            workspace: bool = True,
            timeout: float = _TIMEOUT_SECONDS,
            **environment: str,
        ) -> subprocess.CompletedProcess[str]:
            scope = ["--gateway", _GATEWAY]
            if workspace:
                scope += ["--workspace", self._route.workspace]
            return self._call(
                [str(self._cli), *arguments, *scope], {**base, **environment}, timeout
            )

        added = self._call(
            [str(self._cli), "gateway", "add", endpoint, "--name", _GATEWAY, "--local"],
            base,
            _TIMEOUT_SECONDS,
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
            found = self._connected(run, "workspace", "get", route.workspace, workspace=False)
            if _unreachable(found):
                _require(found, "reach the session's OpenShell gateway")
            if found.returncode != 0:
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

    def _connected(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]],
        *arguments: str,
        workspace: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run the tool, again with growing pauses while it cannot connect to the gateway.

        No pause and no attempt extends beyond ``_CONNECT_SECONDS`` after the first
        attempt, except by the one second that an attempt may always take. The last
        result is returned.
        """
        deadline = self._clock() + _CONNECT_SECONDS
        pause = _CONNECT_FIRST_PAUSE_SECONDS
        while True:
            timeout = max(min(_TIMEOUT_SECONDS, deadline - self._clock()), 1.0)
            result = run(*arguments, workspace=workspace, timeout=timeout)
            if not _unreachable(result) or deadline - self._clock() <= pause:
                return result
            self._sleep(pause)
            pause = min(pause * 2, _CONNECT_LONGEST_PAUSE_SECONDS)

    def exists(self, endpoint: str, client: dict[str, str]) -> bool:
        """Return whether the session's OpenShell gateway holds the provider.

        Only the gateway's answer that the provider is not found means that it lost the
        provider. A gateway that cannot be reached, for example while it restarts, or any
        other failure raises ``ProviderError``, so that a running session keeps its key.
        """
        with TemporaryDirectory(dir=self._scratch, prefix="openshell-") as name:
            run = self._session(Path(name), endpoint, client)
            found = self._connected(run, "provider", "get", self._route.name)
            if found.returncode == 0:
                return True
            if _unreachable(found):
                raise _error(found, "reach the session's OpenShell gateway")
            if "not found" in found.stderr:
                return False
            raise _error(found, "look up the provider")


def _unreachable(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether the tool could not connect to the gateway at all."""
    return result.returncode != 0 and "connect error" in result.stderr


def _error(result: subprocess.CompletedProcess[str], action: str) -> ProviderError:
    # The tool reports errors without credentials; its last line names the cause.
    lines = result.stderr.strip().splitlines()
    cause = lines[-1][:300] if lines else f"exit status {result.returncode}"
    return ProviderError(f"could not {action}: {cause}")


def _require(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode != 0:
        raise _error(result, action)
