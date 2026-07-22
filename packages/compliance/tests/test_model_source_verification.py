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

    assert {source.model_id: (source.repository, source.revision) for source in sources} == {
        "gpt-oss-120b-vllm": (
            "openai/gpt-oss-120b",
            "b5c939de8f754692c1647ca79fbf85e8c1e70f8a",
        ),
        "qwen25-coder-7b-instruct-awq-vllm": (
            "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ",
            "8e8ed243bbe6f9a5aff549a0924562fc719b2b8a",
        ),
        "qwen25-coder-14b-instruct-awq-vllm": (
            "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ",
            "eb3172f06a6d6b3a15f08947b0668d782e4d2d2c",
        ),
        "qwen25-coder-32b-instruct-awq-vllm": (
            "Qwen/Qwen2.5-Coder-32B-Instruct-AWQ",
            "1ed0a6145da0ce550c628e8e8b678f51e695995d",
        ),
        "qwen3-coder-30b-a3b-instruct-fp8-vllm": (
            "Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8",
            "dcaee4d4dfc5ee71ad501f01f530e5652438fde0",
        ),
        "qwen3-coder-next-fp8-vllm": (
            "Qwen/Qwen3-Coder-Next-FP8",
            "da6e2ed27304dd39abadd9c82ef50e8de67bdd4c",
        ),
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
