# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The bucket policies of the job accounts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from heartwood_platform.common.signing import Credentials
from heartwood_platform.jobs.store_policies import set_policies

_POLICY = {"Version": "2012-10-17", "Statement": []}


def test_each_bucket_policy_is_put_with_a_signed_request() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        set_policies(
            {"hw-project-b": _POLICY, "hw-project-a": _POLICY},
            "http://store:7070",
            Credentials("root", "secret"),
            client,
            clock=lambda: datetime(2026, 11, 20, tzinfo=UTC),
        )
    assert [request.url.path for request in requests] == ["/hw-project-a", "/hw-project-b"]
    query = parse_qs(urlsplit(str(requests[0].url)).query, keep_blank_values=True)
    assert query["policy"] == [""]
    assert query["X-Amz-Credential"][0].startswith("root/")
    assert json.loads(requests[0].content) == _POLICY


def test_a_refused_policy_fails_the_bootstrap() -> None:
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client,
        pytest.raises(RuntimeError, match="403"),
    ):
        set_policies({"hw-project-a": _POLICY}, "http://store:7070", Credentials("r", "s"), client)
