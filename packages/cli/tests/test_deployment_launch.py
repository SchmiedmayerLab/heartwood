# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Operator deployment bindings reach the actual gateway launch boundary."""

import socket
from pathlib import Path
from uuid import uuid4

import pytest
import tomli_w
import uvicorn

from heartwood.cli import main
from heartwood.gateway import GatewayAsgiApp
from heartwood.schemas import PolicyProfile


@pytest.mark.parametrize("missing", [False, True])
def test_gateway_launch_uses_external_binding_or_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: bool,
) -> None:
    workspace = tmp_path / "research"
    workspace.mkdir()
    state_parent = tmp_path / "protected"
    state_parent.mkdir(mode=0o700)
    identity = uuid4()
    state = state_parent / str(identity)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<main>Synthetic</main>")
    manifest = tmp_path / "deployment.toml"
    policy = PolicyProfile(policy_id="deployment-test", platform_id="generic")
    if not missing:
        manifest.write_text(
            tomli_w.dumps(
                {
                    "schema_version": "heartwood.project-deployment.v1",
                    "project_id": str(identity),
                    "workspace": str(workspace),
                    "state_root": str(state),
                    "policy": policy.model_dump(exclude_none=True),
                }
            )
        )
        manifest.chmod(0o600)
    started: list[Path] = []

    def run(server: uvicorn.Server, **_kwargs: object) -> None:
        app = server.config.app
        assert isinstance(app, GatewayAsgiApp)
        assert app.gateway.project.project_id == identity
        assert app.gateway.project.state_root == state
        assert app.gateway.config_store.load().policy == policy
        app.gateway.config_store.save(app.gateway.config_store.load())
        started.append(app.gateway.project.state_root)
        app.gateway.stop()

    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    monkeypatch.setattr("heartwood.cli.uvicorn.Server.run", run)
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.Config.bind_socket",
        lambda _config: socket.create_server(("127.0.0.1", 0)),
    )
    result = main(
        [
            "gateway",
            "serve",
            "--deployment",
            str(manifest),
            "--web-root",
            str(web),
            "--ingress-mode",
            "direct-loopback",
        ]
    )
    assert result == (64 if missing else 0)
    assert started == ([] if missing else [state])
    assert not (workspace / ".heartwood").exists()
    assert (state / "config.toml").exists() is not missing


@pytest.mark.parametrize("available", [True, False])
def test_explicit_worker_selection_reaches_native_factory_without_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
) -> None:
    from unittest.mock import Mock

    from heartwood.gateway import ProjectContext, ProjectStateError
    from heartwood.gateway._deployment import ProjectDeployment
    from heartwood.gateway.worker_deployment import OpenShellWorkerDeployment

    research = tmp_path / "research"
    research.mkdir()
    identity = uuid4()
    parent = tmp_path / "state"
    parent.mkdir(mode=0o700)
    project = ProjectContext(research, protected_state=parent / str(identity), project_id=identity)
    # The operator-file validator has independent real-filesystem tests.
    settings = OpenShellWorkerDeployment.model_validate(
        {
            "driver": "openshell-docker",
            "gateway": "synthetic",
            "native_config_home": str(tmp_path / "native"),
            "openshell_cli": str(tmp_path / "openshell"),
            "docker_cli": str(tmp_path / "docker"),
            "docker_host": "unix://" + str(tmp_path / "docker.sock"),
            "workspace": "synthetic",
            "image_digest": "sha256:" + "a" * 64,
            "sandbox_policy_sha256": "b" * 64,
            "policy_path": str(tmp_path / "policy.json"),
            "policy_file_sha256": "c" * 64,
            "profile_id": "synthetic",
            "model": "synthetic",
            "model_endpoint": "https://provider.test/v1/chat/completions",
            "provider": "synthetic",
            "placeholder_env": "SYNTHETIC_PLACEHOLDER",
            "inputs": {},
            "cpu": "1",
            "memory": "2Gi",
        }
    )
    deployment = ProjectDeployment(
        project, PolicyProfile(policy_id="synthetic", platform_id="generic"), settings
    )
    plan_factory = Mock(side_effect=AssertionError("No task was submitted"))
    native_factory = Mock(return_value=plan_factory)
    if not available:
        native_factory.side_effect = ProjectStateError("native runtime unsupported")
    monkeypatch.setattr(
        "heartwood.gateway.worker_native.native_worker_plan_factory", native_factory
    )
    monkeypatch.setattr("heartwood.cli.load_project_deployment", Mock(return_value=deployment))
    monkeypatch.chdir(research)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    web = tmp_path / "web"
    web.mkdir()
    started: list[bool] = []

    def run(server: uvicorn.Server, **_kwargs: object) -> None:
        app = server.config.app
        assert isinstance(app, GatewayAsgiApp)
        assert app.gateway._worker_plan_factory is plan_factory
        started.append(True)
        app.gateway.stop()

    monkeypatch.setattr("heartwood.cli.uvicorn.Server.run", run)
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.Config.bind_socket",
        lambda _config: socket.create_server(("127.0.0.1", 0)),
    )
    result = main(
        [
            "gateway",
            "serve",
            "--deployment",
            str(tmp_path / "deployment.toml"),
            "--web-root",
            str(web),
            "--ingress-mode",
            "direct-loopback",
        ]
    )
    assert result == (0 if available else 64)
    native_factory.assert_called_once_with(settings, project)
    plan_factory.assert_not_called()
    assert started == ([True] if available else [])
    assert not (research / ".heartwood").exists()
