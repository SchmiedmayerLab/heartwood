# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Operator deployment bindings reach the actual gateway launch boundary."""

import json
import socket
from pathlib import Path
from uuid import uuid4

import pytest
import tomli_w
import uvicorn

from heartwood.cli import main
from heartwood.gateway import (
    GatewayAsgiApp,
    ModelCatalogService,
    ProviderModel,
    SessionGateway,
    load_project_deployment,
)
from heartwood.notebook import NotebookSession
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
    policy = PolicyProfile(
        policy_id="deployment-test",
        platform_id="generic",
        allowed_action_review_modes=("review-all", "review-changes"),
        allowed_credential_backends=("process", "keyring", "mounted-file"),
    )
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


@pytest.mark.parametrize("outcome", ["serving", "unsupported", "incapable-gateway"])
def test_explicit_worker_selection_reaches_native_factory_without_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    from unittest.mock import Mock

    from heartwood.core_adapter.worker_attempts import WorkerGatewayCapabilityError

    available = outcome == "serving"

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
    if outcome == "unsupported":
        native_factory.side_effect = ProjectStateError("native runtime unsupported")
    gateway_check = Mock()
    if outcome == "incapable-gateway":
        gateway_check.side_effect = WorkerGatewayCapabilityError("synthetic missing capability")
    monkeypatch.setattr(
        "heartwood.gateway.worker_native.require_gateway_capabilities", gateway_check
    )
    monkeypatch.setattr(
        "heartwood.gateway.worker_native.native_worker_plan_factory", native_factory
    )
    reconciler = Mock(side_effect=AssertionError("No attempt was retained"))
    native_reconciler = Mock(return_value=reconciler)
    monkeypatch.setattr(
        "heartwood.gateway.worker_native.native_worker_reconciler", native_reconciler
    )
    monkeypatch.setattr("heartwood.cli.load_project_deployment", Mock(return_value=deployment))
    monkeypatch.setattr(
        "heartwood.cli.SessionGateway.reconcile_workers",
        lambda _gateway: {
            "session-unobserved": "worker-revocation",
            "session-elsewhere": "worker-control-plane",
        },
    )
    monkeypatch.chdir(research)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    web = tmp_path / "web"
    web.mkdir()
    started: list[bool] = []

    def run(server: uvicorn.Server, **_kwargs: object) -> None:
        app = server.config.app
        assert isinstance(app, GatewayAsgiApp)
        assert app.gateway._worker_plan_factory is plan_factory
        assert app.gateway._worker_reconciler is reconciler
        started.append(True)
        app.gateway.stop()

    monkeypatch.setattr("heartwood.cli.uvicorn.Server.run", run)
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.Config.bind_socket",
        lambda _config: socket.create_server(("127.0.0.1", 0)),
    )
    arguments = [
        "gateway",
        "serve",
        "--deployment",
        str(tmp_path / "deployment.toml"),
        "--web-root",
        str(web),
        "--ingress-mode",
        "direct-loopback",
    ]
    if outcome == "incapable-gateway":
        with pytest.raises(SystemExit, match=r"^HW-WORKER-001: "):
            main(arguments)
    else:
        assert main(arguments) == (0 if available else 64)
    native_factory.assert_called_once_with(settings, project)
    if outcome != "unsupported":
        gateway_check.assert_called_once_with(settings, project)
    plan_factory.assert_not_called()
    if available:
        native_reconciler.assert_called_once_with(settings, project)
    reconciler.assert_not_called()
    reported = capsys.readouterr().err
    assert ("HW-WORKER-003: the worker attempt of session session-unobserved" in reported) is (
        available
    )
    assert ("HW-WORKER-002: the worker attempt of session session-elsewhere" in reported) is (
        available
    )
    assert started == ([True] if available else [])
    assert not (research / ".heartwood").exists()


def test_doctor_browser_and_notebook_report_the_same_manifest_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "research"
    workspace.mkdir()
    (tmp_path / "protected").mkdir(mode=0o700)
    identity = str(uuid4())
    manifest = tmp_path / "deployment.toml"
    manifest.write_text(
        tomli_w.dumps(
            {
                "schema_version": "heartwood.project-deployment.v1",
                "project_id": identity,
                "workspace": str(workspace),
                "state_root": str(tmp_path / "protected" / identity),
                "policy": {
                    "policy_id": "site-policy",
                    "platform_id": "generic",
                    "allowed_action_review_modes": ["review-all"],
                },
            }
        )
    )
    manifest.chmod(0o600)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    assert main(["doctor", "--json", "--deployment", str(manifest)]) == 0
    reported = json.loads(capsys.readouterr().out)["policy"]
    assert reported["source"] == "deployment"
    assert reported["manifest"] == str(manifest)
    assert reported["action_review_modes"] == ["review-all"]
    gateway = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace),
        backend_id="deterministic",
    )
    try:
        assert gateway.project_readiness()["policy"] == reported
        assert gateway.startup_plan(interface="web")["readiness"]["policy"] == reported
        assert NotebookSession(gateway=gateway).project_readiness()["policy"] == reported
    finally:
        gateway.stop()
    assert main(["doctor", "--json"]) == 0
    local = json.loads(capsys.readouterr().out)["policy"]
    assert local["source"] == "platform"
    assert local["manifest"] is None


def test_doctor_reports_a_route_the_manifest_destinations_exclude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "research"
    workspace.mkdir()
    (tmp_path / "protected").mkdir(mode=0o700)
    identity = str(uuid4())
    manifest = tmp_path / "deployment.toml"
    policy: dict[str, object] = {
        "policy_id": "site-policy",
        "platform_id": "generic",
        "allowed_model_endpoints": ["https://api.openai.com/v1/chat/completions"],
        "allowed_model_catalog_endpoints": ["https://api.openai.com/v1/models"],
        "allowed_capability_tiers": ["supervised"],
        "allowed_action_review_modes": ["review-all"],
        "credential_allowlist": ["OPENAI_API_KEY"],
    }

    def write_manifest() -> None:
        manifest.write_text(
            tomli_w.dumps(
                {
                    "schema_version": "heartwood.project-deployment.v1",
                    "project_id": identity,
                    "workspace": str(workspace),
                    "state_root": str(tmp_path / "protected" / identity),
                    "policy": policy,
                }
            )
        )
        manifest.chmod(0o600)

    write_manifest()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    gateway = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace),
        backend_id="deterministic",
        model_catalog_service=ModelCatalogService(
            openai_lister=lambda _connection, _key: (ProviderModel("provider-model"),),
            compatibility=lambda _connection, _model: ("available", "verified", None, True),
        ),
    )
    try:
        gateway.discover_models("openai", token="synthetic-openai-token-value")
        gateway.connect_model("openai", "provider-model")
    finally:
        gateway.stop()

    policy["allowed_data_destinations"] = ["same-compute"]
    write_manifest()
    assert main(["doctor", "--json", "--deployment", str(manifest)]) == 1
    readiness = json.loads(capsys.readouterr().out)
    route = next(
        check for check in readiness["checks"] if check["check_id"] == "configuration-coherence"
    )
    assert route["status"] == "fail"
    assert route["code"] == "HW-SETUP-002"
    assert "deployment policy" in route["summary"]
    assert "data destination provider is not allowed" in route["summary"]
    assert readiness["policy"]["data_destinations"] == ["same-compute"]


def test_doctor_browser_and_serve_report_a_review_mode_the_manifest_narrowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "research"
    workspace.mkdir()
    (tmp_path / "protected").mkdir(mode=0o700)
    identity = str(uuid4())
    manifest = tmp_path / "deployment.toml"

    def write_manifest(*modes: str) -> None:
        manifest.write_text(
            tomli_w.dumps(
                {
                    "schema_version": "heartwood.project-deployment.v1",
                    "project_id": identity,
                    "workspace": str(workspace),
                    "state_root": str(tmp_path / "protected" / identity),
                    "policy": {
                        "policy_id": "site-policy",
                        "platform_id": "generic",
                        "allowed_action_review_modes": list(modes),
                    },
                }
            )
        )
        manifest.chmod(0o600)

    write_manifest("review-all", "review-changes")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    gateway = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace),
        backend_id="deterministic",
    )
    try:
        gateway.select_action_review_mode("review-changes")
    finally:
        gateway.stop()

    write_manifest("review-all")
    assert main(["doctor", "--json", "--deployment", str(manifest)]) == 0
    reported = json.loads(capsys.readouterr().out)
    checks = {check["check_id"]: check for check in reported["checks"]}
    assert checks["configuration"]["status"] == "pass"
    review = checks["action-review"]
    assert review["status"] == "warning"
    assert review["code"] == "HW-SETUP-003"
    assert "Review Changes" in review["summary"]
    assert reported["state"] != "recovery-required"
    assert reported["policy"]["action_review_modes"] == ["review-all"]

    browser = SessionGateway(
        deployment=load_project_deployment(manifest, project_root=workspace),
        backend_id="deterministic",
    )
    try:
        assert browser.project_readiness()["checks"] == reported["checks"]
        assert browser.startup_plan(interface="web")["readiness"]["checks"] == reported["checks"]
        assert browser.action_settings()["review_mode"] == "review-all"
    finally:
        browser.stop()

    # The gateway serves the project and reports the narrowed mode when it starts.
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<main>Synthetic</main>")
    served: list[str] = []

    def run(server: uvicorn.Server, **_kwargs: object) -> None:
        app = server.config.app
        assert isinstance(app, GatewayAsgiApp)
        served.append(str(app.gateway.action_settings()["review_mode"]))
        app.gateway.stop()

    monkeypatch.setattr("heartwood.cli.uvicorn.Server.run", run)
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.Config.bind_socket",
        lambda _config: socket.create_server(("127.0.0.1", 0)),
    )
    arguments = ["gateway", "serve", "--deployment", str(manifest), "--web-root", str(web)]
    assert main([*arguments, "--ingress-mode", "direct-loopback"]) == 0
    assert served == ["review-all"]
    assert f"HW-SETUP-003: {review['summary']}" in capsys.readouterr().err


def _identity_launch(tmp_path: Path, *, identity_table: bool) -> list[str]:
    workspace = tmp_path / "research"
    workspace.mkdir()
    state_parent = tmp_path / "protected"
    state_parent.mkdir(mode=0o700)
    project_id = str(uuid4())
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<main>Synthetic</main>")
    secret = tmp_path / "client-secret"
    secret.write_text("synthetic-client-secret-0123456789\n")
    secret.chmod(0o600)
    actor_key = secret.with_name("actor-key")
    actor_key.write_text("synthetic-actor-key-0123456789abcdef\n")
    actor_key.chmod(0o600)
    record: dict[str, object] = {
        "schema_version": "heartwood.project-deployment.v1",
        "project_id": project_id,
        "workspace": str(workspace),
        "state_root": str(state_parent / project_id),
        "policy": {"policy_id": "deployment-test", "platform_id": "generic"},
    }
    if identity_table:
        record["identity"] = {
            "issuer": "https://id.example.test/realms/research",
            "client_id": "hw-proj-demo",
            "client_secret_file": str(secret),
            "actor_key_file": str(actor_key),
            "subject": "researcher-7f3c",
        }
    manifest = tmp_path / "deployment.toml"
    manifest.write_text(tomli_w.dumps(record))
    manifest.chmod(0o600)
    return ["gateway", "serve", "--deployment", str(manifest), "--web-root", str(web)]


_IDENTITY_ROUTE = [
    "--ingress-mode",
    "identity-proxy",
    "--host",
    "0.0.0.0",
    "--public-origin",
    "https://research.example.test",
    "--base-path",
    "/user/researcher/demo",
    "--trusted-proxy-source",
    "10.0.0.0/24",
]


def test_identity_proxy_launch_serves_the_external_url_without_a_launch_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from heartwood.gateway import IdentityAccess

    arguments = _identity_launch(tmp_path, identity_table=True)
    started: list[bool] = []

    def run(server: uvicorn.Server, **_kwargs: object) -> None:
        app = server.config.app
        assert isinstance(app, GatewayAsgiApp)
        assert isinstance(app.access, IdentityAccess)
        assert app.ingress.mode == "identity-proxy"
        started.append(True)
        app.gateway.stop()

    monkeypatch.chdir(tmp_path / "research")
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    monkeypatch.setattr("heartwood.cli.uvicorn.Server.run", run)
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.Config.bind_socket",
        lambda _config: socket.create_server(("127.0.0.1", 0)),
    )

    assert main([*arguments, *_IDENTITY_ROUTE]) == 0

    printed = capsys.readouterr().out
    assert started == [True]
    assert "https://research.example.test/user/researcher/demo/\n" in printed
    assert "#launch=" not in printed
    assert "synthetic-client-secret" not in printed


@pytest.mark.parametrize(
    ("identity_table", "route", "message"),
    [
        (
            False,
            _IDENTITY_ROUTE,
            r"HW-INGRESS-001.*requires the deployment manifest's \[identity\]",
        ),
        (True, [], r"HW-INGRESS-001.*\[identity\] table requires identity-proxy ingress"),
        (
            True,
            [*_IDENTITY_ROUTE, "--capability-fd", "3"],
            r"HW-INGRESS-001.*accepts no capability descriptor",
        ),
        (True, [*_IDENTITY_ROUTE, "--proxy-strips-prefix"], r"HW-INGRESS-001.*must preserve"),
    ],
)
def test_identity_proxy_launch_refuses_an_incomplete_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity_table: bool,
    route: list[str],
    message: str,
) -> None:
    arguments = _identity_launch(tmp_path, identity_table=identity_table)
    monkeypatch.chdir(tmp_path / "research")
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.Server.run",
        lambda *_args, **_kwargs: pytest.fail("the gateway was started"),
    )

    with pytest.raises(SystemExit, match=message):
        main([*arguments, *route])


def test_platforms_other_than_the_generic_one_do_not_offer_identity_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = tmp_path / "web"
    web.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "carina")

    with pytest.raises(SystemExit, match=r"does not allow identity-proxy gateway ingress"):
        main(["gateway", "serve", "--web-root", str(web), *_IDENTITY_ROUTE])
