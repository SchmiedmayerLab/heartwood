# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""A researcher's browser at the Hub and at a Heartwood gateway behind the Hub's proxy.

The browser is curl with one cookie jar in a probe pod of heartwood-sessions. Passwords
and capabilities travel on standard input or in files that only the probe reads, never
on a command line.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from identity import PLATFORM, SESSIONS, curl, secret, write_private
from kube import Cluster

ORIGIN = "https://proxy-public.heartwood-platform.svc.cluster.local"
SESSION_LABEL = "heartwood.platform/session"


@dataclass
class Browser:
    """One researcher's browser, with its own cookies."""

    cluster: Cluster
    pod: str
    username: str
    jar: str = field(default_factory=lambda: f"/scratch/{secrets.token_hex(8)}.cookies")

    def get(self, url: str, *extra: str) -> tuple[int, str, str]:
        """Request a page without following redirects; return status, Location and body."""
        output = curl(
            self.cluster,
            self.pod,
            "--cookie",
            self.jar,
            "--cookie-jar",
            self.jar,
            "--header",
            "Sec-Fetch-Mode: navigate",
            "--header",
            "Sec-Fetch-Dest: document",
            *extra,
            "--write-out",
            "\n%{http_code} %{redirect_url}",
            url if url.startswith("http") else ORIGIN + url,
        )
        body, _, last = output.rpartition("\n")
        status, _, location = last.partition(" ")
        return int(status), location, body

    def cookie(self, name: str) -> str:
        result = self.cluster.kubectl(
            "exec", f"--namespace={SESSIONS}", self.pod, "--", "cat", self.jar
        )
        assert result.returncode == 0, result.stderr
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 7 and parts[5] == name:
                return parts[6]
        raise AssertionError(f"no cookie {name}")

    def keycloak(self, location: str) -> str:
        """Sign in at the Keycloak page behind ``location``; return the callback URL."""
        status, redirect, page = self.get(location)
        if status == 302:
            # Keycloak already holds this browser's sign-in and returns at once.
            return redirect
        assert status == 200, page[:300]
        form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page)
        assert form is not None, page[:300]
        password = secret(self.cluster, PLATFORM, "keycloak-researchers", self.username)
        output = curl(
            self.cluster,
            self.pod,
            "--cookie",
            self.jar,
            "--cookie-jar",
            self.jar,
            "--data",
            "@-",
            "--write-out",
            "\n%{http_code} %{redirect_url}",
            html.unescape(form.group(1)),
            text_input=urlencode({"username": self.username, "password": password}),
        )
        status_text, _, callback = output.rpartition("\n")[2].partition(" ")
        assert status_text == "302", output[-300:]
        return callback

    def sign_in_to_hub(self) -> None:
        """Sign in at the Hub through Keycloak."""
        status, location, _ = self.get("/hub/oauth_login")
        assert status == 302, location
        callback = self.keycloak(location)
        status, location, body = self.get(callback)
        assert status == 302, body[:300]

    def spawn(self, researcher: str, project: str) -> tuple[int, str]:
        """Start the named server of a project with the spawn form; return the answer.

        The status is the one of the form's submission, or 0 when no form was shown.
        """
        status, location, page = self.get(f"/hub/spawn/{researcher}/{project}")
        if status != 200:
            # The Hub showed no form and started nothing, for example while it still stops
            # the server: the answer is 0 with the location or page.
            return 0, location or page
        xsrf = self.cookie("_xsrf")
        output = curl(
            self.cluster,
            self.pod,
            "--cookie",
            self.jar,
            "--cookie-jar",
            self.jar,
            "--data",
            "@-",
            "--write-out",
            "\n%{http_code} %{redirect_url}",
            f"{ORIGIN}/hub/spawn/{researcher}/{project}",
            text_input=urlencode({"_xsrf": xsrf, "profile": project}),
        )
        body, _, last = output.rpartition("\n")
        return int(last.partition(" ")[0]), body

    def hub_api(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        """Call the Hub's REST API with the browser's session."""
        xsrf = self.cookie("_xsrf")
        data = ["--data-binary", "@-"] if body is not None else []
        output = curl(
            self.cluster,
            self.pod,
            "--cookie",
            self.jar,
            "--request",
            method,
            "--header",
            f"X-XSRFToken: {xsrf}",
            *data,
            "--write-out",
            "\n%{http_code}",
            f"{ORIGIN}/hub/api{path}",
            text_input=json.dumps(body) if body is not None else "",
        )
        answer, _, status = output.rpartition("\n")
        try:
            return int(status), json.loads(answer)
        except ValueError:
            return int(status), answer

    def sign_in_to_gateway(self, base: str) -> tuple[int, str, str]:
        """Sign in at a gateway behind the proxy; return the gateway's callback answer."""
        status, location, _ = self.get(f"{base}/")
        assert status in {302, 303, 307}, (status, location)
        status, location, _ = self.get(location)
        assert status in {302, 303, 307}, (status, location)
        callback = self.keycloak(location)
        return self.get(callback, "--header", "Sec-Fetch-Site: cross-site")

    def launch(self, base: str) -> str:
        """Sign in at a gateway and exchange the launch token for a capability."""
        status, location, body = self.sign_in_to_gateway(base)
        assert status in {302, 303}, (status, body[:300])
        token = parse_qs(urlsplit(location).fragment)["launch"][0]
        status, answer = self.api(base, "POST", "/launch", None, {"token": token})
        assert status == 200, answer
        capability = answer["capability"]
        assert isinstance(capability, str)
        return capability

    def api(
        self,
        base: str,
        method: str,
        path: str,
        capability: str | None,
        body: dict[str, Any] | None = None,
        max_time: int = 60,
    ) -> tuple[int, Any]:
        """Call the gateway's API through the proxy, waiting at most ``max_time`` seconds."""
        lines = ["Accept: application/json"]
        if capability is not None:
            lines.append(f"X-Heartwood-Capability: {capability}")
        if body is not None:
            lines.append("Content-Type: application/json")
        headers = write_private(self.cluster, self.pod, "\n".join(lines))
        arguments = [
            "--request",
            method,
            "--header",
            f"@{headers}",
            "--write-out",
            "\n%{http_code}",
        ]
        if body is not None:
            arguments += ["--data-binary", "@-"]
        output = curl(
            self.cluster,
            self.pod,
            *arguments,
            f"{ORIGIN}{base}{path}",
            text_input=json.dumps(body) if body is not None else "",
            max_time=max_time,
        )
        text, _, status = output.rpartition("\n")
        try:
            return int(status), json.loads(text)
        except ValueError:
            return int(status), text[:300]


def gateway_pods(cluster: Cluster, session: str | None = None) -> list[dict[str, Any]]:
    selector = SESSION_LABEL if session is None else f"{SESSION_LABEL}={session}"
    result = cluster.kubectl(
        "get", f"--namespace={SESSIONS}", "pods", f"--selector={selector}", "--output=json"
    )
    assert result.returncode == 0, result.stderr
    items: list[dict[str, Any]] = json.loads(result.stdout)["items"]
    return items


def wait_for_gateway(browser: Browser, base: str, timeout: float = 420) -> None:
    """Wait until the proxy routes to the gateway and the gateway answers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _, _ = browser.get(f"{base}/")
        # The gateway redirects a browser without a sign-in; the Hub answers otherwise.
        if status in {302, 303, 307}:
            location = browser.get(f"{base}/")[1]
            if "identity/login" in location:
                return
        time.sleep(3)
    raise AssertionError(f"the gateway at {base} did not answer")
