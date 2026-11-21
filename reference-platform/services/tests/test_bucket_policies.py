# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The one writer of the project buckets' policies, and the grants the chart renders."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from heartwood_platform.common.bucket_policies import Grant, PolicySettings, write_policies
from pydantic import ValidationError
from test_chart import _named, _render

_NOW = datetime(2026, 11, 20, tzinfo=UTC)


def _settings(tmp_path: Path, buckets: dict[str, Any]) -> PolicySettings:
    for name, value in {"root-access": "root", "root-secret": "s", "data-access": "data\n"}.items():
        (tmp_path / name).write_text(value)
    return PolicySettings.model_validate(
        {
            "endpoint": "https://versitygw:7070",
            "region": "us-east-1",
            "ca_file": str(tmp_path / "ca.crt"),
            "root_access_key_file": str(tmp_path / "root-access"),
            "root_secret_key_file": str(tmp_path / "root-secret"),
            "buckets": buckets,
        }
    )


def _grants(tmp_path: Path, project: str) -> list[dict[str, Any]]:
    return [
        {"account_file": str(tmp_path / "data-access"), "actions": ["s3:GetObject"]},
        {"account": f"hw-jobs-{project}", "actions": ["s3:GetObject"]},
        {"account": f"hw-jobs-{project}", "actions": ["s3:PutObject"], "prefix": "results/"},
    ]


def test_each_bucket_gets_one_policy_with_the_statements_of_every_account(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        {"hw-project-b": _grants(tmp_path, "b"), "hw-project-a": _grants(tmp_path, "a")},
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        write_policies(settings, client, lambda: _NOW)

    # One write for each bucket, so no account's statements replace another's.
    assert [(request.method, request.url.path) for request in requests] == [
        ("PUT", "/hw-project-a"),
        ("PUT", "/hw-project-b"),
    ]
    query = parse_qs(urlsplit(str(requests[0].url)).query, keep_blank_values=True)
    assert query["policy"] == [""]
    assert query["X-Amz-Credential"][0].startswith("root/")
    assert json.loads(requests[0].content) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["data"]},
                "Action": ["s3:GetObject"],
                "Resource": ["arn:aws:s3:::hw-project-a/*"],
            },
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["hw-jobs-a"]},
                "Action": ["s3:GetObject"],
                "Resource": ["arn:aws:s3:::hw-project-a/*"],
            },
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["hw-jobs-a"]},
                "Action": ["s3:PutObject"],
                "Resource": ["arn:aws:s3:::hw-project-a/results/*"],
            },
        ],
    }


@pytest.mark.parametrize("content", ["", " \n", "two words", "*"])
def test_an_account_file_without_an_access_key_writes_no_policy(
    tmp_path: Path, content: str
) -> None:
    settings = _settings(tmp_path, {"hw-project-a": _grants(tmp_path, "a")})
    (tmp_path / "data-access").write_text(content)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(ValueError, match="no access key"),
    ):
        write_policies(settings, client, lambda: _NOW)
    assert requests == []


def test_a_refused_policy_fails_the_bootstrap(tmp_path: Path) -> None:
    settings = _settings(tmp_path, {"hw-project-a": _grants(tmp_path, "a")})
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client,
        pytest.raises(RuntimeError, match="403"),
    ):
        write_policies(settings, client, lambda: _NOW)


@pytest.mark.parametrize(
    "grant",
    [
        {"actions": ["s3:GetObject"]},
        {"account": "a", "account_file": "/a", "actions": ["s3:GetObject"]},
        {"account": "a", "actions": []},
        {"account": "a", "actions": ["s3:DeleteObject"]},
        {"account": "a", "actions": ["s3:PutObject"], "prefix": "results"},
        {"account": "a", "actions": ["s3:PutObject"], "prefix": "../"},
    ],
)
def test_a_grant_names_one_account_known_actions_and_a_plain_prefix(grant: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Grant.model_validate(grant)


def test_only_project_buckets_with_grants_and_an_https_store_are_accepted(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError):
        _settings(tmp_path, {"other-bucket": _grants(tmp_path, "a")})
    with pytest.raises(ValidationError):
        _settings(tmp_path, {"hw-project-a": []})
    settings = _settings(tmp_path, {"hw-project-a": _grants(tmp_path, "a")})
    with pytest.raises(ValidationError):
        PolicySettings.model_validate(
            {**settings.model_dump(), "endpoint": "http://versitygw:7070"}
        )


def test_the_chart_grants_the_data_and_job_accounts_on_every_project_bucket() -> None:
    objects = _render("values-kind.yaml")
    config = _named(objects, "ConfigMap", "heartwood-bucket-policies")
    settings = PolicySettings.model_validate_json(config["data"]["bucket-policies.json"])
    projects = ("demo", "other", "small")
    assert sorted(settings.buckets) == [f"hw-project-{project}" for project in projects]
    for project in projects:
        assert settings.buckets[f"hw-project-{project}"] == (
            Grant(
                account_file=Path("/etc/heartwood-store/data/access-key"), actions=("s3:GetObject",)
            ),
            Grant(account=f"hw-jobs-{project}", actions=("s3:GetObject",)),
            Grant(account=f"hw-jobs-{project}", actions=("s3:PutObject",), prefix="results/"),
        )
    job = _named(objects, "Job", "heartwood-bucket-policies")
    # It runs after the hooks that create the data account (5) and the job accounts (1).
    assert job["metadata"]["annotations"]["helm.sh/hook-weight"] == "10"
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["args"][0] == "heartwood_platform.common.bucket_policies"
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["data"]["secret"] == {
        "secretName": "versitygw-data",
        "items": [{"key": "access-key", "path": "access-key"}],
    }
    # No other Job writes a bucket policy.
    for other in (
        _named(objects, "Job", "heartwood-data-bootstrap"),
        _named(objects, "Job", "heartwood-jobs-store"),
    ):
        commands = [
            argument
            for container in other["spec"]["template"]["spec"]["containers"]
            for argument in container.get("args", [])
        ]
        assert "policy" not in " ".join(commands)
