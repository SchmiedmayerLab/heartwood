# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest

from heartwood.gateway import (
    ProjectContext,
    ReadinessCheck,
    diagnostic_catalog,
    diagnostic_for,
    persist_deployment_profile,
    plan_startup,
)


def test_new_project_plan_explains_first_review_without_writing_state(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)

    plan = plan_startup(project, interface="terminal", env={})

    assert plan.phase == "project-review"
    assert plan.interface_supported is True
    assert plan.access_url is None
    assert plan.capabilities.display_name == "Workstation or container"
    assert not project.state_root.exists()
    serialized = plan.safe_dict()
    readiness = cast(dict[str, object], serialized["readiness"])
    checks = cast(list[dict[str, object]], readiness["checks"])
    attention = next(check for check in checks if check["status"] != "pass")
    code = attention["code"]
    assert isinstance(code, str)
    assert code.startswith("HW-")


def test_generic_web_plan_returns_one_direct_url(tmp_path: Path) -> None:
    plan = plan_startup(ProjectContext(tmp_path), interface="web", port=9000, env={})

    assert plan.access_url == "http://127.0.0.1:9000/"
    assert plan.capabilities.browser_route == "direct"


def test_setup_plan_distinguishes_connection_and_model_selection(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()

    without_connection = plan_startup(project, env={})
    persist_deployment_profile(project, model_source="openai", env={})
    without_model = plan_startup(project, env={})

    assert without_connection.phase == "connection-required"
    assert without_model.phase == "model-required"


def test_terra_rejects_the_unsupported_web_interface(tmp_path: Path) -> None:
    project = tmp_path / "jupyter" / "analysis"
    project.mkdir(parents=True)
    base_env = {
        "HEARTWOOD_PLATFORM": "terra",
        "HEARTWOOD_PLATFORM_HOME": str(tmp_path / "jupyter"),
    }

    plan = plan_startup(
        ProjectContext(project),
        interface="web",
        env={**base_env, "JUPYTERHUB_SERVICE_PREFIX": "/user/synthetic/"},
    )

    assert plan.phase == "recovery-required"
    assert plan.interface_supported is False
    assert plan.next_action == "Use the terminal interface in this environment."
    assert plan.access_url is None


def test_carina_rejects_an_unsupported_web_interface(tmp_path: Path) -> None:
    plan = plan_startup(
        ProjectContext(tmp_path),
        interface="web",
        env={"HEARTWOOD_PLATFORM": "carina"},
    )

    assert plan.phase == "recovery-required"
    assert plan.interface_supported is False
    assert plan.next_action == "Use the terminal interface in this environment."
    assert plan.access_url is None


def test_startup_plan_rejects_invalid_ports(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="port must be between"):
        plan_startup(ProjectContext(tmp_path), port=0)


def test_diagnostic_catalog_is_unique_and_sorted() -> None:
    diagnostics = diagnostic_catalog()
    codes = [item.code for item in diagnostics]

    assert codes == sorted(codes)
    assert len(codes) == len(set(codes))
    assert all(item.documentation_path.startswith("/") for item in diagnostics)
    assert all(re.fullmatch(r"HW-[A-Z]+-[0-9]{3}", code) for code in codes)
    assert all(
        1 <= int(code.rsplit("-", 1)[1]) <= 899 for code in codes if not code.startswith("HW-ENV-")
    )
    assert diagnostic_for("unclassified-check").code == "HW-ENV-999"
    assert diagnostic_for("model-source").code == "HW-MODEL-001"
    assert "code" not in ReadinessCheck("project-storage", "pass", "Ready").safe_dict()
