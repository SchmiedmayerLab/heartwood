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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from identity import PLATFORM, SESSIONS, curl, private_file, researcher, secret
from kube import Cluster

ORIGIN = "https://proxy-public.heartwood-platform.svc.cluster.local"
SESSION_LABEL = "heartwood.platform/session"


def project_url(path: str) -> str:
    _, _, researcher_id, project, *_ = path.split("/")
    return f"https://{researcher_id}.{project}.projects.heartwood.test{path}"


@dataclass
class Browser:
    """One researcher's browser, with its own cookies."""

    cluster: Cluster
    pod: str
    username: str
    jar: str = field(default_factory=lambda: f"/scratch/{secrets.token_hex(8)}.cookies")

    def get(self, url: str, *extra: str) -> tuple[int, str, str]:
        """Request a page without following redirects; return status, Location and body."""
        if url.startswith("/user/"):
            url = project_url(url)
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
        if result.returncode:
            raise AssertionError("Could not read the browser cookie jar")
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
        assert status == 200
        form = re.search(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"', page)
        if form is None:
            raise AssertionError("Missing login form")
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
        assert status_text == "302"
        return callback

    def sign_in_to_hub(self) -> None:
        """Sign in at the Hub through Keycloak."""
        status, location, _ = self.get("/hub/oauth_login")
        assert status == 302
        callback = self.keycloak(location)
        status, _, _ = self.get(callback)
        assert status == 302

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
        with private_file(self.cluster, self.pod, f"X-XSRFToken: {xsrf}") as headers:
            output = curl(
                self.cluster,
                self.pod,
                "--cookie",
                self.jar,
                "--request",
                method,
                "--header",
                f"@{headers}",
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
        assert status in {302, 303, 307}
        status, location, _ = self.get(location)
        assert status in {302, 303, 307}
        expected = project_url(base) + "/oauth_callback"
        assert parse_qs(urlsplit(location).query).get("redirect_uri") == [expected]
        callback = self.keycloak(location)
        assert callback.partition("?")[0] == expected
        return self.get(callback, "--header", "Sec-Fetch-Site: cross-site")

    def launch(self, base: str) -> str:
        """Sign in at a gateway and exchange the launch token for a capability."""
        status, location, _ = self.sign_in_to_gateway(base)
        assert status in {302, 303}
        token = parse_qs(urlsplit(location).fragment)["launch"][0]
        status, answer = self.api(base, "POST", "/launch", None, {"token": token})
        assert status == 200
        capability = answer["capability"]
        if not isinstance(capability, str):
            raise AssertionError("Invalid gateway capability response")
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
        with private_file(self.cluster, self.pod, "\n".join(lines)) as headers:
            arguments = [
                "--cookie",
                self.jar,
                "--cookie-jar",
                self.jar,
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
                project_url(base + path),
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


@dataclass
class Server:
    """One named server of a researcher, opened through the Hub's proxy."""

    browser: Browser
    subject: str
    project: str
    session: str = ""
    capability: str = field(default="", repr=False)
    conversation: str = ""

    @property
    def user(self) -> str:
        return researcher(self.subject)

    @property
    def base(self) -> str:
        return f"/user/{self.user}/{self.project}"

    def pod(self) -> dict[str, Any]:
        (pod,) = gateway_pods(self.browser.cluster, self.session)
        return pod

    def start(self) -> None:
        status, _ = self.browser.spawn(self.user, self.project)
        assert status == 302
        self.wait_started()

    def wait_started(self) -> None:
        """Finish connecting after the Hub accepts a spawn."""
        wait_for_gateway(self.browser, self.base)
        (pod,) = [
            pod
            for pod in gateway_pods(self.browser.cluster)
            if pod["metadata"]["labels"].get("hub.jupyter.org/username") == self.user
            and pod["metadata"]["labels"].get("hub.jupyter.org/servername") == self.project
        ]
        self.session = pod["metadata"]["labels"][SESSION_LABEL]
        self.capability = self.browser.launch(self.base)

    def stop(self) -> None:
        self.browser.hub_api("DELETE", f"/users/{self.user}/servers/{self.project}")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status, user = self.browser.hub_api("GET", f"/users/{self.user}")
            stopped = status == 200 and self.project not in user.get("servers", {})
            if stopped and not gateway_pods(self.browser.cluster, self.session):
                return
            time.sleep(3)
        raise AssertionError("the Hub did not stop the server")

    def remove(self) -> None:
        """Delete the named server, which deletes its session, and wait until it is gone.

        The Hub stops the server and deletes the session after it answers, so the next
        start of a server of the same name would otherwise race the deletion. The Hub
        deletes the server's claim last.
        """
        self.browser.hub_api(
            "DELETE", f"/users/{self.user}/servers/{self.project}", {"remove": True}
        )
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            status, user = self.browser.hub_api(
                "GET", f"/users/{self.user}?include_stopped_servers=1"
            )
            gone = status == 200 and self.project not in user.get("servers", {})
            if gone and not (self.session and self._session_objects()):
                return
            time.sleep(3)
        raise AssertionError("the Hub did not delete the server")

    def _session_objects(self) -> list[str]:
        """The gateway pods, Secret and claim of the server's session that still exist."""
        result = self.browser.cluster.kubectl(
            "get",
            f"--namespace={SESSIONS}",
            "pods",
            f"--selector={SESSION_LABEL}={self.session}",
            "--output=name",
        )
        assert result.returncode == 0, result.stderr
        named = self.browser.cluster.kubectl(
            "get",
            f"--namespace={SESSIONS}",
            f"secret/hw-gw-{self.session}",
            f"persistentvolumeclaim/hw-home-{self.session}",
            "--output=name",
            "--ignore-not-found",
        )
        assert named.returncode == 0, named.stderr
        namespace = self.browser.cluster.kubectl(
            "get", f"namespace/hw-s-{self.session}", "--output=name", "--ignore-not-found"
        )
        if namespace.returncode:
            raise AssertionError("Could not verify worker namespace removal")
        return [*result.stdout.split(), *named.stdout.split(), *namespace.stdout.split()]

    def api(
        self, method: str, path: str, body: dict[str, Any] | None = None, max_time: int = 60
    ) -> tuple[int, Any]:
        return self.browser.api(self.base, method, path, self.capability, body, max_time)

    def connect(self, route: str, model: str) -> dict[str, Any]:
        """Select a model of the route and create a conversation."""
        status, catalog = self.api(
            "POST", "/settings/models/catalog", {"connection_id": route, "refresh": True}
        )
        assert status == 200
        status, _ = self.api(
            "POST", "/settings/models/connect", {"connection_id": route, "model_id": model}
        )
        assert status == 200
        self.create_conversation()
        if not isinstance(catalog, dict):
            raise AssertionError("Invalid model catalog response")
        return catalog

    def create_conversation(self) -> None:
        status, created = self.api("POST", "/sessions", {"title": "Model route"})
        assert status == 201
        self.conversation = created["session_id"]

    def command(self, kind: str, payload: dict[str, Any]) -> None:
        body = {
            "schema_version": "heartwood.session-command.v1",
            "command_id": f"{self.conversation}-{kind}-{uuid4().hex}",
            "session_id": self.conversation,
            "kind": kind,
            "created_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        # A command can start the session's worker before it answers.
        status, _ = self.api("POST", f"/sessions/{self.conversation}/commands", body, max_time=600)
        assert status == 200

    def snapshot(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return the conversation's events and the gateway's projection of them."""
        status, answer = self.api("GET", f"/sessions/{self.conversation}/events")
        assert status == 200
        return answer["events"], answer["projection"]

    def events(self) -> list[dict[str, Any]]:
        return self.snapshot()[0]

    def wait(self, found: Callable[[list[dict[str, Any]]], Any], timeout: float = 420) -> Any:  # noqa: ANN401
        """Read the conversation's events until ``found`` returns a value."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.events()
            result = found(events)
            if result:
                return result
            time.sleep(3)
        raise AssertionError("Timed out waiting for the conversation observation")


def events_of(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event for event in events if event["kind"] == kind]


def pending_group(events: list[dict[str, Any]]) -> str | None:
    """Return the first action set that waits for the researcher's decision."""
    resolved = {
        event["payload"].get("group_id") for event in events_of(events, "confirmation.resolved")
    }
    for event in events_of(events, "confirmation.requested"):
        group = event["payload"].get("request", {}).get("group_id")
        if group and group not in resolved:
            return str(group)
    return None


def error_codes(events: list[dict[str, Any]]) -> list[str]:
    return [str(event["payload"].get("code")) for event in events_of(events, "error.recorded")]
