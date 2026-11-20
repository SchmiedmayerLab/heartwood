# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Checks of the data service, the object store and project data in the rendered chart."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from heartwood_platform.control.__main__ import Settings as ControlSettings
from heartwood_platform.data.__main__ import Settings
from heartwood_platform.data.seed import SeedSettings
from test_chart import _CHART, _TEMPLATE, _named, _render

_SERVICES = "registry.example.test/services@sha256:" + "0" * 64


@pytest.fixture(scope="module")
def objects() -> list[dict[str, Any]]:
    return _render("values-kind.yaml")


def _configuration(objects: list[dict[str, Any]]) -> Settings:
    data = _named(objects, "ConfigMap", "heartwood-data")["data"]
    return Settings.model_validate_json(data["configuration.json"])


@pytest.mark.parametrize("service", ["heartwood-data", "versitygw"])
def test_the_data_service_and_the_object_store_have_certificates_of_the_platform_ca(
    objects: list[dict[str, Any]], tmp_path: Path, service: str
) -> None:
    authority = tmp_path / "ca.crt"
    authority.write_bytes(
        base64.b64decode(_named(objects, "Secret", "platform-ca")["data"]["tls.crt"])
    )
    serving = tmp_path / "serving.crt"
    serving.write_bytes(
        base64.b64decode(_named(objects, "Secret", f"{service}-tls")["data"]["tls.crt"])
    )
    subprocess.run(
        ["openssl", "verify", "-CAfile", str(authority), str(serving)],
        check=True,
        capture_output=True,
    )
    text = subprocess.run(
        ["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(serving)],
        check=True,
        capture_output=True,
    ).stdout.decode()
    for name in (service, f"{service}.heartwood-platform.svc.cluster.local"):
        assert f"DNS:{name}," in f"{text.strip()},"


def test_the_object_store_serves_only_https(objects: list[dict[str, Any]]) -> None:
    spec = _named(objects, "StatefulSet", "versitygw")["spec"]["template"]["spec"]
    (container,) = spec["containers"]
    arguments = container["args"]
    assert arguments[arguments.index("--cert") + 1] == "/etc/versitygw/tls/tls.crt"
    assert arguments[arguments.index("--key") + 1] == "/etc/versitygw/tls/tls.key"
    assert container["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    for job in ("versitygw-bootstrap", "heartwood-data-bootstrap"):
        pod = _named(objects, "Job", job)["spec"]["template"]["spec"]
        for item in pod.get("initContainers", []) + pod["containers"]:
            endpoints = [
                variable["value"]
                for variable in item.get("env", [])
                if variable["name"] == "ADMIN_ENDPOINT_URL"
            ]
            assert all(endpoint.startswith("https://") for endpoint in endpoints)
    assert _configuration(objects).object_store.endpoint.startswith("https://")


def test_the_catalog_holds_each_dataset_file_with_its_size_and_checksum(
    objects: list[dict[str, Any]],
) -> None:
    catalog = _configuration(objects).catalog
    for project in catalog.projects:
        for entry in catalog.entries(project.id):
            content = (_CHART / "files" / "datasets" / entry.path).read_bytes()
            assert entry.item.size == len(content)
            assert entry.item.sha256 == hashlib.sha256(content).hexdigest()
    stored = _named(objects, "ConfigMap", "heartwood-data-objects")["data"]
    assert stored["synthetic-cohort.participants.csv"] == (
        _CHART / "files/datasets/synthetic-cohort/participants.csv"
    ).read_text(encoding="utf-8")
    seed = SeedSettings.model_validate_json(
        _named(objects, "ConfigMap", "heartwood-data")["data"]["seed.json"]
    )
    assert {item.file.name for item in seed.objects} == set(stored)
    assert seed.buckets == ("hw-project-demo", "hw-project-other", "hw-project-small")


def test_a_dataset_file_that_is_missing_fails_the_rendering(tmp_path: Path) -> None:
    values = tmp_path / "values.yaml"
    values.write_text(
        json.dumps(
            {
                "projects": [
                    {
                        "id": "demo",
                        "billingAttributionRef": "grant",
                        "policyRef": "policy",
                        "datasets": [
                            {
                                "id": "missing",
                                "name": "Missing",
                                "description": "No files.",
                                "created": "2026-11-01T00:00:00Z",
                                "files": ["absent.csv"],
                            }
                        ],
                    }
                ]
            }
        )
    )
    result = subprocess.run(
        [*_TEMPLATE, "--values", str(values)], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "files/datasets/missing/absent.csv" in result.stderr


def test_the_object_store_accepts_only_its_jobs_and_copy_pods(
    objects: list[dict[str, Any]],
) -> None:
    policies = [
        item
        for item in objects
        if item["kind"] == "NetworkPolicy"
        and item["spec"]["podSelector"].get("matchLabels", {}).get("app.kubernetes.io/component")
        == "versitygw"
    ]
    sources = [
        source
        for policy in policies
        for rule in policy["spec"]["ingress"]
        for source in rule["from"]
    ]
    components = {
        source["podSelector"]["matchLabels"].get("app.kubernetes.io/component")
        for source in sources
        if "namespaceSelector" not in source
    }
    assert components == {"versitygw-bootstrap", "data-bootstrap"}
    (session,) = [source for source in sources if "namespaceSelector" in source]
    assert session == {
        "namespaceSelector": {
            "matchExpressions": [{"key": "heartwood.platform/session", "operator": "Exists"}]
        },
        "podSelector": {"matchLabels": {"heartwood.platform/role": "project-data-copy"}},
    }


def test_the_data_service_reviews_tokens_and_holds_only_the_read_account(
    objects: list[dict[str, Any]],
) -> None:
    role = _named(objects, "ClusterRole", "heartwood-data")
    assert role["rules"] == [
        {"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]}
    ]
    spec = _named(objects, "Deployment", "heartwood-data")["spec"]["template"]["spec"]
    secrets = {volume["secret"]["secretName"] for volume in spec["volumes"] if "secret" in volume}
    assert secrets == {"versitygw-data", "heartwood-data-tls"}
    settings = _configuration(objects)
    assert settings.service_callers == {
        "system:serviceaccount:heartwood-platform:heartwood-control"
    }
    # Only the bootstrap Job holds the root account.
    holders = {
        name
        for name, pod in (
            (item["metadata"]["name"], item["spec"]["template"]["spec"])
            for item in objects
            if item["kind"] in {"Deployment", "StatefulSet", "Job"}
        )
        if any(
            volume.get("secret", {}).get("secretName") == "versitygw-root"
            for volume in pod.get("volumes", [])
        )
        or any(
            variable.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == "versitygw-root"
            for container in pod["containers"] + pod.get("initContainers", [])
            for variable in container.get("env", [])
        )
    }
    assert holders == {"versitygw", "versitygw-bootstrap", "heartwood-data-bootstrap"}


def test_the_control_service_fills_project_data_with_the_services_image(
    objects: list[dict[str, Any]],
) -> None:
    configuration = _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    settings = ControlSettings.model_validate_json(configuration)
    assert settings.project_data is not None
    assert settings.project_data.copy_image == _SERVICES
    assert settings.project_data.storage_class == "heartwood-project-data"
    assert settings.project_data.object_store_labels["app.kubernetes.io/component"] == "versitygw"
    spec = _named(objects, "StatefulSet", "heartwood-control")["spec"]["template"]["spec"]
    (token,) = [volume for volume in spec["volumes"] if volume["name"] == "data-token"]
    assert token["projected"]["sources"][0]["serviceAccountToken"]["audience"] == "heartwood-data"
    rules = _named(objects, "ClusterRole", "heartwood-session-control")["rules"]
    listed = [rule for rule in rules if "list" in rule["verbs"]]
    assert {resource for rule in listed for resource in rule["resources"]} == {
        "pods",
        "persistentvolumeclaims",
    }
    storage = _named(objects, "StorageClass", "heartwood-project-data")
    assert storage["provisioner"] == "rancher.io/local-path"
