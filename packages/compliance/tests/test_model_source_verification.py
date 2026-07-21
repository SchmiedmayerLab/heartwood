# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from types import ModuleType

import pytest


def _verifier() -> ModuleType:
    path = Path("deploy/verify_model_sources.py")
    spec = importlib.util.spec_from_file_location("verify_model_sources", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_repository_model_sources_are_immutable_and_use_revision_routes() -> None:
    verifier = _verifier()
    sources = verifier.load_model_sources(Path.cwd())

    assert {source.model_id for source in sources} == {
        "qwen25-7b-instruct-awq-vllm",
        "qwen25-7b-instruct-vllm",
        "qwen3-8b-awq-vllm",
    }
    assert all(len(source.revision) == 40 for source in sources)
    assert all(f"/revision/{source.revision}" in source.api_url for source in sources)


def test_model_source_verification_requires_exact_repository_and_revision() -> None:
    verifier = _verifier()
    source = verifier.ModelSource(
        "synthetic-model",
        "example/model",
        "a" * 40,
    )

    verifier.verify_model_source(
        source,
        fetch_json=lambda _url, _timeout: {"id": "example/model", "sha": "a" * 40},
    )
    verifier.verify_model_source(
        source,
        fetch_json=lambda _url, _timeout: {"modelId": "example/model", "sha": "a" * 40},
    )
    with pytest.raises(verifier.ModelSourceVerificationError, match="expected revision"):
        verifier.verify_model_source(
            source,
            fetch_json=lambda _url, _timeout: {"id": "example/model", "sha": "b" * 40},
        )
    with pytest.raises(verifier.ModelSourceVerificationError, match="expected repository"):
        verifier.verify_model_source(
            source,
            fetch_json=lambda _url, _timeout: {"id": "other/model", "sha": "a" * 40},
        )


def test_model_source_fetch_distinguishes_missing_revisions_from_provider_outages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _verifier()
    monkeypatch.setattr(verifier.time, "sleep", lambda _delay: None)

    def unavailable(_request: object, *, timeout: float) -> object:
        del timeout
        raise urllib.error.URLError("synthetic outage")

    monkeypatch.setattr(verifier.urllib.request, "urlopen", unavailable)
    with pytest.raises(verifier.ModelSourceUnavailableError, match="synthetic outage"):
        verifier._fetch_json("https://huggingface.co/api/models/example/model", 1)

    def missing(request: object, *, timeout: float) -> object:
        del timeout
        raise urllib.error.HTTPError(str(request), 404, "Not Found", Message(), None)

    monkeypatch.setattr(verifier.urllib.request, "urlopen", missing)
    with pytest.raises(verifier.ModelSourceVerificationError, match="HTTP 404"):
        verifier._fetch_json("https://huggingface.co/api/models/example/model", 1)

    def timeout(request: object, *, timeout: float) -> object:
        del timeout
        raise urllib.error.HTTPError(str(request), 408, "Request Timeout", Message(), None)

    monkeypatch.setattr(verifier.urllib.request, "urlopen", timeout)
    with pytest.raises(verifier.ModelSourceUnavailableError, match="HTTP Error 408"):
        verifier._fetch_json("https://huggingface.co/api/models/example/model", 1)
