# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

_PLATFORM = Path(__file__).resolve().parents[2]


def _tool() -> ModuleType:
    path = _PLATFORM / "tools" / "render_openshell.py"
    spec = importlib.util.spec_from_file_location("render_openshell", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _tool()


def _committed() -> list[dict[str, Any]]:
    items = json.loads(tool.OUTPUT.read_text(encoding="utf-8"))["items"]
    assert isinstance(items, list)
    return items


def _chart_output(objects: list[dict[str, Any]]) -> str:
    return yaml.safe_dump_all(objects)


def _certgen(kind: str) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": kind,
        "metadata": {"name": "openshell-certgen", "namespace": tool.NAMESPACE_PLACEHOLDER},
    }


def test_committed_render_is_stable_under_the_tool() -> None:
    committed = _committed()

    assert tool.session_objects(_chart_output(committed)) == committed
    assert tool.serialize(committed) == tool.OUTPUT.read_text(encoding="utf-8")


def test_committed_render_holds_only_namespaced_objects_without_rbac() -> None:
    committed = _committed()

    assert {item["kind"] for item in committed} == {
        "ConfigMap",
        "Job",
        "NetworkPolicy",
        "Service",
        "ServiceAccount",
        "StatefulSet",
    }
    assert {item["metadata"]["namespace"] for item in committed} == {tool.NAMESPACE_PLACEHOLDER}
    text = tool.OUTPUT.read_text(encoding="utf-8")
    assert "helm.sh/hook" not in text
    assert text.count(tool.GATEWAY_IMAGE_PLACEHOLDER) == 2


def test_certificate_job_rbac_is_removed_and_namespaces_are_added() -> None:
    objects = _committed()
    for item in objects:
        if item["kind"] != "NetworkPolicy":
            del item["metadata"]["namespace"]
    rendered = _chart_output([*objects, _certgen("Role"), _certgen("RoleBinding")])

    assert tool.session_objects(rendered) == _committed()


@pytest.mark.parametrize("kind", ["Role", "ClusterRole", "ClusterRoleBinding", "Secret"])
def test_unexpected_object_fails(kind: str) -> None:
    extra = {"apiVersion": "v1", "kind": kind, "metadata": {"name": "extra"}}

    with pytest.raises(tool.RenderError, match=f"unexpected object {kind}/extra"):
        tool.session_objects(_chart_output([*_committed(), extra]))


def test_new_namespace_occurrence_fails() -> None:
    objects = _committed()
    objects[0].setdefault("data", {})["peer"] = f"openshell.{tool.NAMESPACE_PLACEHOLDER}.svc"

    with pytest.raises(tool.RenderError, match="namespace occurrences changed"):
        tool.session_objects(_chart_output(objects))


def test_other_image_fails() -> None:
    objects = _committed()
    job = next(item for item in objects if item["kind"] == "Job")
    job["spec"]["template"]["spec"]["containers"][0]["image"] = "example-gateway:1"

    with pytest.raises(tool.RenderError, match="unexpected image example-gateway:1"):
        tool.session_objects(_chart_output(objects))


def test_other_chart_archive_fails(tmp_path: Path) -> None:
    archive = tmp_path / "chart.tgz"
    archive.write_bytes(b"not the pinned chart")

    with pytest.raises(tool.RenderError, match="chart archive has SHA-256"):
        tool.render(archive)


def test_gateway_passes_the_platform_ca_to_sandbox_supervisors() -> None:
    objects = {(item["kind"], item["metadata"]["name"]): item for item in _committed()}
    spec = objects[("StatefulSet", "openshell")]["spec"]["template"]["spec"]
    (volume,) = [item for item in spec["volumes"] if item["name"] == "additional-ca-source"]
    (mount,) = [
        item
        for item in spec["containers"][0]["volumeMounts"]
        if item["name"] == "additional-ca-source"
    ]
    configuration = objects[("ConfigMap", "openshell-config")]["data"]["gateway.toml"]

    assert volume["configMap"] == {
        "name": "platform-ca",
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
    }
    assert mount["mountPath"] == "/etc/openshell-tls/additional-ca-source/ca.crt"
    assert (
        '[openshell.supervisor.network]\nadditional_ca_bundle = "/etc/openshell-tls/'
        'additional-ca-source/ca.crt"' in configuration
    )


def test_gateway_has_the_settings_the_platform_requirements_name() -> None:
    objects = {(item["kind"], item["metadata"]["name"]): item for item in _committed()}
    configuration = tomllib.loads(
        objects[("ConfigMap", "openshell-config")]["data"]["gateway.toml"]
    )
    driver = configuration["openshell"]["drivers"]["kubernetes"]
    release = json.loads(
        (_PLATFORM.parent / "images" / "worker" / "candidate" / "gateway.json").read_text()
    )["images"]

    assert objects[("StatefulSet", "openshell")]["spec"]["replicas"] == 1
    assert driver["allow_driver_config"] is True
    assert driver["allowed_pod_labels"] == [
        "heartwood.project",
        "heartwood.attempt",
        "heartwood.role",
    ]
    assert (driver["sandbox_uid"], driver["sandbox_gid"]) == (10001, 10001)
    assert driver["workspace_mode"] == "shared"
    assert driver["supervisor_image"] == release["supervisor"]
    assert driver["sandbox_runtime_image"] == release["sandbox"]
    # Creations without a recorded outcome keep their request admission slot.
    assert configuration["openshell"]["gateway"]["mutation_admission_limit_per_caller"] == 100000
