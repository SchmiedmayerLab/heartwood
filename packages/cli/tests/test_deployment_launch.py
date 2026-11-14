# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Operator deployment bindings reach the actual gateway launch boundary."""

from pathlib import Path
from uuid import uuid4

import pytest
import tomli_w

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

    def run(app: GatewayAsgiApp, **_kwargs: object) -> None:
        assert app.gateway.project.project_id == identity
        assert app.gateway.project.state_root == state
        assert app.gateway.config_store.load().policy == policy
        app.gateway.config_store.save(app.gateway.config_store.load())
        started.append(app.gateway.project.state_root)
        app.gateway.stop()

    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "generic")
    monkeypatch.setattr("heartwood.cli.uvicorn.run", run)
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
