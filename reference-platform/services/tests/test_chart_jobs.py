# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Checks of the rendered job service and the Funnel executor of each project."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
import yaml
from heartwood_platform.jobs.__main__ import Settings
from test_chart import _named, _render

_PROJECTS = ("demo", "other", "small")
_SERVER_RULES = [
    {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "list", "get", "delete"]},
    {"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": ["create"]},
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create", "delete"]},
]
_WORKER_RULES = [
    {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create"]},
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["watch"]},
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
]


@pytest.fixture(scope="module")
def objects() -> list[dict[str, Any]]:
    return _render("values-kind.yaml")


def _server_config(objects: list[dict[str, Any]], project: str) -> dict[str, Any]:
    secret = _named(objects, "Secret", "funnel-server-config", f"hw-jobs-{project}")
    config = yaml.safe_load(base64.b64decode(secret["data"]["server.yaml"]))
    assert isinstance(config, dict)
    return config


def _worker_config(objects: list[dict[str, Any]], project: str) -> dict[str, Any]:
    config_map = _named(objects, "ConfigMap", "funnel-worker-config", f"hw-jobs-{project}")
    config = yaml.safe_load(config_map["data"]["worker.yaml"])
    assert isinstance(config, dict)
    return config


@pytest.mark.parametrize("project", _PROJECTS)
def test_each_project_has_a_restricted_jobs_namespace_with_quota_and_default_deny(
    objects: list[dict[str, Any]], project: str
) -> None:
    namespace = f"hw-jobs-{project}"
    labels = _named(objects, "Namespace", namespace)["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    assert labels["heartwood.platform/jobs"] == "true"
    quota = _named(objects, "ResourceQuota", "jobs", namespace)["spec"]["hard"]
    assert {"requests.cpu", "limits.memory", "pods", "requests.storage"} <= set(quota)
    deny = _named(objects, "NetworkPolicy", "default-deny", namespace)["spec"]
    assert deny == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    selected = [
        item["spec"]["podSelector"]
        for item in objects
        if item["kind"] == "NetworkPolicy" and item["metadata"]["namespace"] == namespace
    ]
    # No policy selects executor pods, so they reach nothing and nothing reaches them.
    assert {"matchLabels": {"heartwood.platform/role": "funnel-executor"}} not in selected
    assert len(selected) == 3


def test_the_server_is_reached_by_workers_and_the_job_service_only(
    objects: list[dict[str, Any]],
) -> None:
    policy = _named(objects, "NetworkPolicy", "funnel-server", "hw-jobs-demo")["spec"]
    sources = [(rule["from"], rule["ports"][0]["port"]) for rule in policy["ingress"]]
    assert sources[0] == (
        [{"podSelector": {"matchLabels": {"heartwood.platform/role": "funnel-worker"}}}],
        9090,
    )
    (jobs,) = sources[1][0]
    assert sources[1][1] == 8000
    assert jobs["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "heartwood-platform"}
    }
    assert jobs["podSelector"]["matchLabels"]["app.kubernetes.io/component"] == "jobs"
    worker = _named(objects, "NetworkPolicy", "funnel-worker", "hw-jobs-demo")["spec"]
    assert worker["policyTypes"] == ["Egress"]
    destinations = [rule["ports"][0]["port"] for rule in worker["egress"]]
    # The server's gRPC port, the object store, DNS and the API server.
    assert destinations == [9090, 7070, 53, 443]


@pytest.mark.parametrize("project", _PROJECTS)
def test_roles_grant_the_recorded_verbs_and_no_secret_read(
    objects: list[dict[str, Any]], project: str
) -> None:
    namespace = f"hw-jobs-{project}"
    assert _named(objects, "Role", "funnel-server", namespace)["rules"] == _SERVER_RULES
    assert _named(objects, "Role", "funnel-worker", namespace)["rules"] == _WORKER_RULES
    executor = _named(objects, "ServiceAccount", "funnel-executor", namespace)
    assert executor["automountServiceAccountToken"] is False


@pytest.mark.parametrize("project", _PROJECTS)
def test_the_server_serves_tls_and_issues_per_task_worker_credentials(
    objects: list[dict[str, Any]], project: str
) -> None:
    config = _server_config(objects, project)
    server = config["Server"]
    assert server["TLSCertFile"] == "/etc/funnel-tls/tls.crt"
    assert server["WorkerCredentialKeyFile"] == "/etc/funnel-worker-key/key"
    assert server["TaskAccess"] == "OwnerOrAdmin"
    # The job service is the only Basic user; there is no shared worker user.
    assert [(user["User"], user["Admin"]) for user in server["BasicAuth"]] == [("jobsvc", True)]
    template = yaml.safe_load(
        config["Kubernetes"]["WorkerTemplate"].replace("{{", "").replace("}}", "")
    )
    pod = template["spec"]["template"]["spec"]
    assert pod["hostUsers"] is False
    assert pod["serviceAccountName"] == "funnel-worker"
    # The worker moves files with presigned HTTPS URLs and trusts the platform CA alone.
    (worker,) = pod["containers"]
    assert {"name": "SSL_CERT_FILE", "value": "/etc/platform-ca/ca.crt"} in worker["env"]
    assert "WorkerCredentialSecret" in config["Kubernetes"]["WorkerTemplate"]
    deployment = _named(objects, "Deployment", "funnel-server", f"hw-jobs-{project}")
    volumes = {item["name"]: item for item in deployment["spec"]["template"]["spec"]["volumes"]}
    assert volumes["tls"]["secret"]["secretName"] == "funnel-server-tls"
    assert volumes["worker-key"]["secret"]["secretName"] == "funnel-worker-key"


@pytest.mark.parametrize("project", _PROJECTS)
def test_workers_hold_no_credential_and_executors_no_token(
    objects: list[dict[str, Any]], project: str
) -> None:
    config = _worker_config(objects, project)
    assert config["RPCClient"] == {
        "ServerAddress": f"funnel-server.hw-jobs-{project}.svc:9090",
        "TLSCAFile": "/etc/platform-ca/ca.crt",
        "CredentialFile": "/etc/funnel-task/credential",
    }
    assert "Server" not in config
    template = yaml.safe_load(
        config["Kubernetes"]["ExecutorTemplate"]
        .split("{{- if")[0]
        .replace("{{", "")
        .replace("}}", "")
    )
    pod = template["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["hostUsers"] is False
    assert pod["serviceAccountName"] == "funnel-executor"
    assert "volumes" not in pod
    assert template["metadata"]["labels"]["heartwood.platform/role"] == "funnel-executor"


@pytest.mark.parametrize("project", _PROJECTS)
def test_only_presigned_http_transfers_are_enabled(
    objects: list[dict[str, Any]], project: str
) -> None:
    for config in (_server_config(objects, project), _worker_config(objects, project)):
        assert {
            name: config[name]["Disabled"]
            for name in ("AmazonS3", "GoogleStorage", "Swift", "FTPStorage", "HTTPStorage")
        } == {
            "AmazonS3": True,
            "GoogleStorage": True,
            "Swift": True,
            "FTPStorage": True,
            "HTTPStorage": False,
        }
        assert "GenericS3" not in config


def test_the_job_service_configuration_matches_the_executors(
    objects: list[dict[str, Any]],
) -> None:
    config_map = _named(objects, "ConfigMap", "heartwood-jobs", "heartwood-platform")
    settings = Settings.model_validate_json(config_map["data"]["configuration.json"])
    assert [project.id for project in settings.projects] == list(_PROJECTS)
    assert settings.audience == "heartwood-jobs"
    assert settings.images
    # Presigned URLs name the store over HTTPS, verified against the platform CA.
    assert settings.store.endpoint == "https://versitygw.heartwood-platform.svc.cluster.local:7070"
    assert str(settings.store.ca_file) == "/etc/heartwood-jobs/platform-ca/ca.crt"
    passwords = _named(objects, "Secret", "heartwood-jobs-executors", "heartwood-platform")["data"]
    for project in settings.projects:
        assert project.executor.url == f"https://funnel-server.hw-jobs-{project.id}.svc:8000"
        (user,) = _server_config(objects, project.id)["Server"]["BasicAuth"]
        assert base64.b64decode(passwords[project.id]).decode() == user["Password"]
        assert project.store.bucket == f"hw-project-{project.id}"


def test_the_job_service_is_reached_by_gateways_and_uses_no_api_token(
    objects: list[dict[str, Any]],
) -> None:
    policy = _named(objects, "NetworkPolicy", "heartwood-jobs", "heartwood-platform")["spec"]
    assert policy["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "heartwood-sessions"}
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8443}],
        }
    ]
    pod = _named(objects, "StatefulSet", "heartwood-jobs", "heartwood-platform")["spec"][
        "template"
    ]["spec"]
    assert pod["automountServiceAccountToken"] is False
    (token,) = [item for item in pod["volumes"] if item["name"] == "control-token"]
    (source,) = token["projected"]["sources"]
    assert source["serviceAccountToken"]["audience"] == "heartwood-control"
    control = json.loads(
        _named(objects, "ConfigMap", "heartwood-control", "heartwood-platform")["data"][
            "configuration.json"
        ]
    )
    # The data service and the job service, and no other account by default.
    assert control["context_callers"] == [
        "system:serviceaccount:heartwood-platform:heartwood-data",
        "system:serviceaccount:heartwood-platform:heartwood-jobs",
    ]


def test_worker_created_jobs_are_confined_to_their_own_task(
    objects: list[dict[str, Any]],
) -> None:
    policy = _named(objects, "ValidatingAdmissionPolicy", "heartwood-funnel-worker-jobs")["spec"]
    assert policy["failurePolicy"] == "Fail"
    messages = {item["message"] for item in policy["validations"]}
    assert "worker-created Jobs may mount only their task's workspace claim" in messages
    assert "worker-created Jobs must belong to the task of the worker's pod" in messages
    binding = _named(objects, "ValidatingAdmissionPolicyBinding", "heartwood-funnel-worker-jobs")
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert binding["spec"]["matchResources"]["namespaceSelector"] == {
        "matchLabels": {"heartwood.platform/jobs": "true"}
    }


def test_workers_reach_the_object_store(objects: list[dict[str, Any]]) -> None:
    ingress = _named(objects, "NetworkPolicy", "versitygw", "heartwood-platform")["spec"]["ingress"]
    sources = ingress[0]["from"]
    assert {
        "namespaceSelector": {"matchLabels": {"heartwood.platform/jobs": "true"}},
        "podSelector": {"matchLabels": {"heartwood.platform/role": "funnel-worker"}},
    } in sources


def test_gpus_are_bounded_by_the_quota_of_each_project(objects: list[dict[str, Any]]) -> None:
    for project in _PROJECTS:
        quota = _named(objects, "ResourceQuota", "jobs", f"hw-jobs-{project}")["spec"]["hard"]
        # Neither kind project holds the GPU entitlement.
        assert quota["requests.nvidia.com/gpu"] == "0"
        assert quota["count/jobs.batch"] == "40"
    policy = _named(objects, "ValidatingAdmissionPolicy", "heartwood-funnel-worker-jobs")["spec"]
    assert "worker-created Jobs run one pod once" in {
        item["message"] for item in policy["validations"]
    }


def test_worker_created_jobs_run_without_a_pod_failure_policy(
    objects: list[dict[str, Any]],
) -> None:
    policy = _named(objects, "ValidatingAdmissionPolicy", "heartwood-funnel-worker-jobs")["spec"]
    (rule,) = [
        item["expression"]
        for item in policy["validations"]
        if item["message"] == "worker-created Jobs run one pod once"
    ]
    assert "!has(object.spec.podFailurePolicy)" in rule
    assert "restartPolicy == 'Never'" in rule
    # A container's own restart policy or rules would run it again inside the pod.
    assert "!has(c.restartPolicy) && !has(c.restartPolicyRules)" in rule
