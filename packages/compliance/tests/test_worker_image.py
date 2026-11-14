# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Behavioral checks for the native worker launch profile."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "worker_launch", _ROOT / "images/worker/launch.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_child_receives_only_explicit_environment(launcher: ModuleType) -> None:
    source = {
        "SESSION_API_KEY": "synthetic-attempt-" + "x" * 43,
        "HEARTWOOD_INFERENCE_PLACEHOLDER": "synthetic-placeholder",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret",
        "GH_TOKEN": "repository-secret",
        "HF_TOKEN": "model-secret",
        "JUPYTERHUB_API_TOKEN": "notebook-secret",
        "HEARTWOOD_GATEWAY_CAPABILITY": "gateway-secret",
        "OPENAI_API_KEY": "provider-secret",
        "UNRECOGNIZED_SECRET": "unknown-secret",
        "HTTP_PROXY": "http://unapproved.invalid",
        "HTTPS_PROXY": "http://unapproved.invalid",
        "ALL_PROXY": "http://unapproved.invalid",
        "SSL_CERT_FILE": "/untrusted/cert.pem",
        "PYTHONPATH": "/untrusted/code",
        "LD_PRELOAD": "/untrusted/library.so",
        "BASH_ENV": "/untrusted/startup",
        "OH_ENABLE_VSCODE": "true",
        "OH_TELEMETRY_CONSENT": "granted",
        "OH_TELEMETRY_HTTP_TOKEN": "telemetry-secret",
        "OPENHANDS_AGENT_SERVER_CONFIG_PATH": "/workspace/config.json",
        "HOME": "/host/credential/home",
        "PATH": "/host/bin",
    }
    child = subprocess.run(
        [sys.executable, "-I", "-c", "import json,os;print(json.dumps(dict(os.environ)))"],
        env=launcher.worker_environment(source),
        close_fds=True,
        capture_output=True,
        text=True,
        check=True,
    )
    environment = json.loads(child.stdout)
    assert environment["SESSION_API_KEY"] == source["SESSION_API_KEY"]
    assert environment["HEARTWOOD_INFERENCE_PLACEHOLDER"] == "synthetic-placeholder"
    assert environment["HOME"] == "/home/heartwood"
    assert environment["OH_TELEMETRY_CONSENT"] == "denied"
    assert environment["OPENHANDS_AGENT_SERVER_CONFIG_PATH"] == "/etc/heartwood/worker.json"
    for name in source.keys() - {
        "SESSION_API_KEY",
        "HEARTWOOD_INFERENCE_PLACEHOLDER",
        "HOME",
        "PATH",
        "OH_TELEMETRY_CONSENT",
        "OPENHANDS_AGENT_SERVER_CONFIG_PATH",
    }:
        assert name not in environment
    assert source["PATH"] not in environment["PATH"]


@pytest.mark.parametrize("key", ["", "short", "a" * 42, "a" * 257, "a" * 43 + "\n"])
def test_worker_rejects_missing_or_invalid_attempt_key(launcher: ModuleType, key: str) -> None:
    with pytest.raises(ValueError, match="attempt key"):
        launcher.worker_environment({"SESSION_API_KEY": key})


@pytest.mark.parametrize("placeholder", ["", "x" * 4097, "x\0y", "x\ny", "x\ry"])
def test_worker_rejects_invalid_placeholder(launcher: ModuleType, placeholder: str) -> None:
    with pytest.raises(ValueError, match="placeholder"):
        launcher.worker_environment(
            {
                "SESSION_API_KEY": "a" * 43,
                "HEARTWOOD_INFERENCE_PLACEHOLDER": placeholder,
            }
        )


def test_native_server_consumes_fixed_profile(
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The server is optional in the application install, but required for worker qualification.
    config_module = pytest.importorskip("openhands.agent_server.config")
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name, value in launcher.worker_environment({"SESSION_API_KEY": "a" * 43}).items():
        monkeypatch.setenv(name, value)
    config = config_module.load_config(_ROOT / "images/worker/config.json")
    assert config.session_api_keys == ["a" * 43]
    assert config.max_concurrent_runs == 1
    assert config.conversation_runtime == "local"
    assert not config.conversation_runtime_selectable
    assert not config.enable_browser
    assert not config.enable_vscode
    assert not config.preload_tools
    assert not config.trust_forwarded_headers
    assert config.telemetry.exporter == "none"
    assert config.webhooks == []
    assert config.registered_marketplaces == []
    assert config.workspace_path == Path("/workspace")
    assert config.conversations_path.is_relative_to("/home/heartwood/.local/state/worker")
