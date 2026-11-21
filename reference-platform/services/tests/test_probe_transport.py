# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Local checks of the signed-link probes using synthetic kubectl responses."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import test_project_data as probes
from identity import private_file
from kube import Cluster
from test_estimates import Researcher

_URL = f"{probes._STORE}/bucket/object?X-Amz-Signature=synthetic-private-value"


def _result(output: str, code: int = 0) -> CompletedProcess[str]:
    return CompletedProcess([], code, stdout=output, stderr=_URL)


@pytest.mark.parametrize(
    ("exit_code", "output", "accepted"),
    [
        (7, "\n000 0.000000", True),
        (28, "\n000 0.000000", True),
        (28, "\n000 0.002300", False),
        (28, "\n000", False),
        (60, "\n000 0.000000", False),
        (1, "", False),
        (28, "\n200 0.003000", False),
        (0, "\n403 0.003000", False),
        (7, _URL, False),
    ],
)
def test_denied_probe_requires_curl_connection_failure(
    exit_code: int, output: str, accepted: bool
) -> None:
    cluster = Mock(spec=Cluster)
    config = f"url = {json.dumps(_URL)}\n"
    cluster.kubectl.side_effect = [
        _result('{"spec":{"clusterIP":"192.0.2.1"}}'),
        _result(str(len(config))),
        _result(output, exit_code),
        _result(""),
    ]
    if accepted:
        probes._fetch(cluster, "selected", "probe", _URL, None)
    else:
        with pytest.raises(AssertionError) as failure:
            probes._fetch(cluster, "selected", "probe", _URL, None)
        assert "synthetic-private-value" not in str(failure.value)
    _, write, fetch, cleanup = cluster.kubectl.call_args_list
    assert write.kwargs == {"text_input": config}
    path = fetch.args[-1]
    assert fetch.args[-2] == "--config"
    assert fetch.kwargs == {}
    assert fetch.args[fetch.args.index("--write-out") + 1] == "\n%{http_code} %{time_connect}"
    assert cleanup.args[-3:] == ("rm", "-f", path)
    for call in (write, fetch, cleanup):
        assert "--namespace=selected" in call.args
        assert _URL not in " ".join(call.args)


def test_http_status_failure_does_not_print_store_response() -> None:
    cluster = Mock(spec=Cluster)
    config = f"url = {json.dumps(_URL)}\n"
    cluster.kubectl.side_effect = [
        _result('{"spec":{"clusterIP":"192.0.2.1"}}'),
        _result(str(len(config))),
        _result(f"{_URL}\n403 0.003000"),
        _result(""),
    ]
    with pytest.raises(AssertionError) as failure:
        probes._fetch(cluster, "selected", "probe", _URL, 200)
    assert "403" in str(failure.value)
    assert "synthetic-private-value" not in str(failure.value)


@pytest.mark.parametrize(
    ("code", "message"),
    [("SignatureDoesNotMatch", _URL), ("AccessDenied", "Request has expired.")],
)
def test_read_only_requires_policy_denial(code: str, message: str) -> None:
    body = (
        f"<Error><Code>{code}</Code><Message>{message}</Message><Resource>{_URL}</Resource></Error>"
    )
    with pytest.raises(AssertionError) as failure:
        probes._require_store_error(body, "AccessDenied", "Access Denied.")
    assert str(failure.value) == f"unexpected store rejection (code {code})"


@pytest.mark.parametrize("stage", ["write", "short", "use", "cleanup"])
def test_one_removal_on_each_failure_path(stage: str) -> None:
    cluster = Mock(spec=Cluster)
    writes: list[CompletedProcess[str] | TimeoutError] = (
        [_result("0")] * 5 if stage == "short" else [_result(str(len(_URL)))]
    )
    if stage == "write":
        writes = [TimeoutError("write interrupted")]
    cluster.kubectl.side_effect = [*writes, _result("", int(stage == "cleanup"))]
    expected = {
        "write": "write interrupted",
        "short": "did not receive",
        "use": "use interrupted",
        "cleanup": "removing /scratch/.*exit 1",
    }

    def use_file() -> None:
        with private_file(cluster, "pod", _URL, namespace="selected"):
            if stage == "use":
                raise TimeoutError("use interrupted")

    with (
        pytest.warns(UserWarning, match="incomplete") if stage == "short" else nullcontext(),
        pytest.raises((AssertionError, TimeoutError), match=expected[stage]) as failure,
    ):
        use_file()
    assert _URL not in str(failure.value)
    calls = cluster.kubectl.call_args_list
    assert len(calls) == len(writes) + 1
    assert calls[-1].args[1:6] == ("--namespace=selected", "pod", "--", "rm", "-f")


@pytest.fixture
def researcher() -> Researcher:
    state = Researcher(Mock(spec=Cluster), "probe")
    state.sessions["demo"] = "session"
    state.tokens["demo"] = {"data": "synthetic"}
    return state


def test_wrong_ttl_reports_no_signed_url(
    researcher: Researcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probes, "request", lambda *_: (200, {"url": _URL}))
    with pytest.raises(AssertionError) as failure:
        probes.test_object_urls_read_one_object_from_copy_pods_only(researcher, "probe-image")
    assert "synthetic-private-value" not in str(failure.value)


@pytest.mark.parametrize("elapsed_seconds", [17, 75])
def test_expiry_wait_accounts_for_elapsed_probe_time(
    elapsed_seconds: int, researcher: Researcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    elapsed = timedelta(seconds=elapsed_seconds)
    clock = Mock()
    clock.now.side_effect = [start, start, start, start + elapsed]
    sleeper = Mock()
    dataset: Path = probes._DATASETS / "synthetic-cohort" / "measurements.csv"
    seen: list[tuple[str, int | None]] = []

    def fetch(
        _cluster: Cluster,
        _namespace: str,
        _pod: str,
        url: str,
        status: int | None,
        *_args: str,
        **_kwargs: object,
    ) -> str:
        if "/README.md?" in url:
            seen.append((url, status))
            return (
                "<Error><Code>AccessDenied</Code><Message>Request has expired.</Message></Error>"
                if status == 403
                else "readme"
            )
        if "participants.csv" in url:
            return "<Error><Code>SignatureDoesNotMatch</Code></Error>"
        if status == 403:
            return "<Error><Code>AccessDenied</Code><Message>Access Denied.</Message></Error>"
        return dataset.read_text() if status == 200 else ""

    monkeypatch.setattr(probes, "datetime", clock)
    monkeypatch.setattr(probes, "time", sleeper)
    monkeypatch.setattr(probes, "_fetch", fetch)
    monkeypatch.setattr(probes, "_copy_pod", lambda *_: nullcontext("copy"))
    monkeypatch.setattr(probes, "_session_pod", lambda *_: nullcontext("session"))
    monkeypatch.setattr(
        probes,
        "request",
        lambda *_: (
            200,
            {
                "url": _URL.replace("/bucket/object", "/bucket/measurements.csv")
                + "&X-Amz-Expires=301"
            },
        ),
    )
    monkeypatch.setattr(probes, "secret", lambda *_: "synthetic")
    probes.test_object_urls_read_one_object_from_copy_pods_only(researcher, "probe-image")
    (before, first_status), (after, second_status) = seen
    assert before == after
    assert (first_status, second_status) == (200, 403)
    lifetime = int(parse_qs(urlsplit(before).query)["X-Amz-Expires"][0])
    sleeper.sleep.assert_called_once_with(max(0, lifetime + 1 - elapsed_seconds))
