# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""JupyterHub configuration of the Heartwood reference platform.

The Hub loads this file from its jupyterhub_config.d directory, with the configuration
object ``c`` defined. Researchers sign in through Keycloak; each named server is one
researcher's Heartwood gateway for one project, and its name is the project. Before a
spawn the Hub asks the control service to prepare the session, then starts the gateway
pod in the sessions namespace with the session's ServiceAccount and Secret.

A researcher's user name is the researcher identifier that the control service also
derives: ``r`` and 32 hexadecimal digits of the HMAC-SHA-256 of the issuer, a zero byte
and the subject under the platform's researcher identifier key. So no subject appears
in the Hub's URLs, the gateways' base paths, the redirect URIs or the logs.

The functions above ``configure`` use only the standard library, so the chart's tests
import this file without JupyterHub.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any

# A directory mount of the chart's ConfigMap, which kubelet updates after an upgrade.
SETTINGS = Path("/usr/local/etc/heartwood/hub/config/settings.json")
SESSION_LABEL = "heartwood.platform/session"
ROLE_LABEL = "heartwood.platform/role"
CONFIGURE = "/usr/local/libexec/heartwood-hosted-configure"
_PROJECT = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
# The researcher identifier form that the control service accepts in its paths.
_RESEARCHER = re.compile(r"^r[0-9a-f]{32}$")


def researcher_id(issuer: str, subject: str, key: bytes) -> str:
    """Return the researcher identifier of a subject, as the control service derives it."""
    digest = hmac.new(key, f"{issuer}\0{subject}".encode(), hashlib.sha256).hexdigest()
    return f"r{digest[:32]}"


def launch_path(researcher: str, project: str) -> str | None:
    """Return the control service's launch path of a researcher's project server.

    A server name may hold any character, including an encoded slash, so only names of
    the project form, and user names of the researcher identifier form, reach a path.
    """
    if not _RESEARCHER.fullmatch(researcher) or not _PROJECT.fullmatch(project):
        return None
    return f"/v1/researchers/{researcher}/projects/{project}/launch"


def profiles(projects: list[str], server: str) -> list[dict[str, Any]]:
    """Offer the project of a named server, when the researcher is a member of it."""
    return [
        {"display_name": project, "slug": project, "default": True}
        for project in projects
        if project == server
    ]


def gateway_arguments(settings: dict[str, Any], researcher: str, project: str) -> list[str]:
    """Return the arguments of ``heartwood gateway serve`` for one named server."""
    arguments = [
        "--deployment",
        "/etc/heartwood/deployment/deployment.toml",
        "--ingress-mode",
        "identity-proxy",
        "--host",
        "0.0.0.0",
        "--port",
        "8888",
        "--public-origin",
        settings["public_origin"],
        "--base-path",
        f"/user/{researcher}/{project}",
    ]
    for source in settings["trusted_proxy_sources"]:
        arguments += ["--trusted-proxy-source", source]
    return arguments


def pod_settings(launch: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Return the per-session settings of the gateway pod for a prepared launch.

    Only the init container mounts the session's Secret. It copies the files into a
    memory volume with owner-only modes and creates the research and state directories
    on the server's volume, which the gateway mounts by subdirectory. The mounts come
    from the chart, which admits exactly these.
    """
    session = launch["session"]
    secret = launch["gateway_service_account"]

    def mounts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": item["name"],
                "mountPath": item["mountPath"],
                **({"subPath": item["subPath"]} if item["subPath"] else {}),
                **({"readOnly": True} if item["readOnly"] else {}),
            }
            for item in items
        ]

    return {
        "service_account": secret,
        "pvc_name": f"hw-home-{session}",
        "labels": {SESSION_LABEL: session, ROLE_LABEL: "hosted-gateway"},
        "volumes": [
            {"name": "source", "secret": {"secretName": secret, "defaultMode": 0o400}},
            {"name": "work", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}},
            {"name": "home", "persistentVolumeClaim": {"claimName": f"hw-home-{session}"}},
            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
        ],
        "volume_mounts": mounts(settings["volume_mounts"]),
        "init_mounts": mounts(settings["init_mounts"]),
    }


def configure(c: Any, settings: dict[str, Any]) -> None:  # noqa: ANN401
    """Configure the Hub, its authenticator and its spawner."""
    from kubespawner import KubeSpawner
    from tornado import web
    from tornado.httpclient import AsyncHTTPClient, HTTPRequest

    issuer = settings["issuer"]
    origin = settings["public_origin"]

    def platform_ca_only(curl: Any) -> None:  # noqa: ANN401
        """Trust only the CA file: libcurl also reads its built-in certificate directory."""
        import pycurl

        curl.unsetopt(pycurl.CAPATH)

    async def control(method: str, path: str) -> tuple[int, Any]:
        """Call the control service with the Hub's token, trusting the platform CA alone."""
        token = Path(settings["token_file"]).read_text(encoding="utf-8").strip()
        request = HTTPRequest(
            settings["control_url"] + path,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            body="" if method == "PUT" else None,
            allow_nonstandard_methods=method == "DELETE",
            ca_certs=settings["ca_file"],
            validate_cert=True,
            prepare_curl_callback=platform_ca_only,
            request_timeout=300,
        )
        response = await AsyncHTTPClient().fetch(request, raise_error=False)
        try:
            return response.code, json.loads(response.body or b"null")
        except ValueError:
            return response.code, None

    async def projects(researcher: str) -> list[str]:
        if not _RESEARCHER.fullmatch(researcher):
            return []
        status, answer = await control("GET", f"/v1/researchers/{researcher}/projects")
        if status != 200 or not isinstance(answer, dict):
            return []
        return [item for item in answer.get("projects", []) if isinstance(item, str)]

    async def profile_list(spawner: Any) -> list[dict[str, Any]]:  # noqa: ANN401
        return profiles(await projects(spawner.user.name), spawner.name)

    async def pre_spawn_hook(spawner: Any) -> None:  # noqa: ANN401
        # The current settings, so a spawn after an upgrade uses the upgraded values.
        current = json.loads(SETTINGS.read_text(encoding="utf-8"))
        researcher, project = spawner.user.name, spawner.name
        if not _RESEARCHER.fullmatch(researcher):
            raise web.HTTPError(403, "This account cannot start servers.")
        if not _PROJECT.fullmatch(project or ""):
            raise web.HTTPError(400, "Start a named server for one of your projects.")
        chosen = (spawner.user_options or {}).get("profile")
        if chosen not in (None, project):
            raise web.HTTPError(400, "The server name must be the chosen project.")
        path = launch_path(researcher, project)
        if path is None:
            raise web.HTTPError(400, "Start a named server for one of your projects.")
        status, launch = await control("PUT", path)
        if status in {403, 404}:
            raise web.HTTPError(403, "You are not a member of this project.")
        if status != 200 or not isinstance(launch, dict):
            raise web.HTTPError(502, "The platform could not prepare the session.")
        pod = pod_settings(launch, current)
        resources = current["resources"]
        spawner.image = current["gateway_image"]
        spawner.cpu_limit = resources["cpu_limit"]
        spawner.cpu_guarantee = resources["cpu_guarantee"]
        spawner.mem_limit = resources["memory_limit"]
        spawner.mem_guarantee = resources["memory_guarantee"]
        spawner.storage_capacity = current["storage"]["capacity"]
        if current["storage"]["storage_class"]:
            spawner.storage_class = current["storage"]["storage_class"]
        spawner.service_account = pod["service_account"]
        if spawner.pvc_name != pod["pvc_name"]:
            # A new session of the server needs its own claim.
            spawner._pvc_exists = False
        spawner.pvc_name = pod["pvc_name"]
        spawner.extra_labels = {**spawner.extra_labels, **pod["labels"]}
        spawner.volumes = pod["volumes"]
        spawner.volume_mounts = pod["volume_mounts"]
        spawner.init_containers = [
            {
                "name": "configuration",
                "image": current["gateway_image"],
                "command": [CONFIGURE],
                "securityContext": current["container_security_context"],
                "volumeMounts": pod["init_mounts"],
                "resources": current["init_resources"],
            }
        ]
        spawner.args = gateway_arguments(current, researcher, project)

    class HeartwoodSpawner(KubeSpawner):  # type: ignore[misc]
        """A KubeSpawner whose named servers are Heartwood gateways."""

        async def delete_forever(self) -> None:
            """Remove the session at the control service with the named server."""
            # A name outside the project form never started a server, so it has no session.
            path = launch_path(self.user.name, self.name or "")
            if path is not None:
                status, _ = await control("DELETE", path)
                if status not in {200, 204, 404}:
                    raise web.HTTPError(502, "The platform could not remove the session.")
            await super().delete_forever()

    c.JupyterHub.authenticator_class = "generic-oauth"
    c.GenericOAuthenticator.client_id = settings["client_id"]
    c.GenericOAuthenticator.client_secret = (
        Path(settings["client_secret_file"]).read_text(encoding="utf-8").strip()
    )
    c.GenericOAuthenticator.oauth_callback_url = f"{origin}/hub/oauth_callback"
    c.GenericOAuthenticator.authorize_url = f"{issuer}/protocol/openid-connect/auth"
    c.GenericOAuthenticator.token_url = f"{issuer}/protocol/openid-connect/token"
    c.GenericOAuthenticator.userdata_url = f"{issuer}/protocol/openid-connect/userinfo"
    c.GenericOAuthenticator.scope = ["openid"]
    # Read as bytes, as the control service reads the key, so both use the same key.
    researcher_key = Path(settings["researcher_id_key_file"]).read_bytes().removesuffix(b"\n")

    def username(user_info: dict[str, Any]) -> str:
        """Name a signed-in researcher by identifier; the subject stays out of the Hub."""
        subject = user_info.get("sub")
        if not isinstance(subject, str) or not subject:
            # The error names nothing of the user's data, so none of it reaches the log.
            raise web.HTTPError(403, "The sign-in names no subject.")
        return researcher_id(issuer, subject, researcher_key)

    c.GenericOAuthenticator.username_claim = username
    c.GenericOAuthenticator.validate_server_cert = True
    c.GenericOAuthenticator.http_request_kwargs = {
        "ca_certs": settings["ca_file"],
        "prepare_curl_callback": platform_ca_only,
    }
    # Every account of the realm may sign in; the control service authorizes projects.
    c.GenericOAuthenticator.allow_all = True

    c.JupyterHub.allow_named_servers = True
    c.JupyterHub.named_server_limit_per_user = 0
    c.JupyterHub.spawner_class = HeartwoodSpawner
    c.KubeSpawner.namespace = settings["namespace"]
    c.KubeSpawner.profile_list = profile_list
    c.KubeSpawner.pre_spawn_hook = pre_spawn_hook
    c.KubeSpawner.image_pull_policy = "IfNotPresent"
    c.KubeSpawner.cmd = ["heartwood", "gateway", "serve"]
    c.KubeSpawner.port = 8888
    c.KubeSpawner.working_dir = "/research"
    c.KubeSpawner.environment = {"HOME": "/tmp"}
    c.KubeSpawner.uid = 10001
    c.KubeSpawner.gid = 10001
    c.KubeSpawner.fs_gid = 10001
    c.KubeSpawner.allow_privilege_escalation = False
    c.KubeSpawner.automount_service_account_token = True
    c.KubeSpawner.container_security_context = settings["container_security_context"]
    c.KubeSpawner.pod_security_context = {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
        "fsGroupChangePolicy": "OnRootMismatch",
    }
    c.KubeSpawner.storage_pvc_ensure = True
    c.KubeSpawner.storage_access_modes = ["ReadWriteOnce"]
    c.KubeSpawner.start_timeout = 300
    c.KubeSpawner.http_timeout = 120


if "c" in globals():
    configure(c, json.loads(SETTINGS.read_text(encoding="utf-8")))  # type: ignore[name-defined]  # noqa: F821
