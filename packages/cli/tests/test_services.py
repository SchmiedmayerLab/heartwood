# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The terminal adapts the gateway's research MCP server operations."""

from __future__ import annotations

import argparse
import io
from pathlib import Path
from uuid import uuid4

import pytest

from heartwood.cli import main
from heartwood.cli._services import configure_services, format_mcp_test, handle_services
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._mcp_policy import McpDestination
from heartwood.schemas import McpServerTestResponse, api_response


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="heartwood services")
    configure_services(parser)
    return parser


def _gateway(tmp_path: Path) -> SessionGateway:
    root = tmp_path / "project"
    root.mkdir()
    identity = uuid4()
    gateway = SessionGateway(
        project=ProjectContext(
            root, protected_state=tmp_path / "protected" / str(identity), project_id=identity
        ),
        mcp_destinations=(
            McpDestination(
                service_id="catalog",
                origin="https://catalog.example.org",
                tools=("find_records",),
                observation_tool="get_operation",
            ),
        ),
        env={},
    )
    gateway.config_store.save(gateway.config_store.load())
    return gateway


def _run(gateway: SessionGateway, *argv: str) -> int:
    parser = _parser()
    return handle_services(parser, gateway, parser.parse_args(argv))


def test_services_add_list_and_disconnect_through_the_gateway(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _gateway(tmp_path)
    try:
        monkeypatch.setattr("sys.stdin", io.StringIO("synthetic-opaque-token\n"))
        assert (
            _run(
                gateway,
                "add",
                "catalog",
                "https://catalog.example.org/mcp",
                "--label",
                "Synthetic catalog",
                "--token-stdin",
            )
            == 0
        )
        listed = capsys.readouterr().out
        assert "catalog  Synthetic catalog  disabled  destination=allowed" in listed
        assert "credential=unknown-expiry" in listed
        assert "synthetic-opaque-token" not in listed
        # The command-line gateway has no worker, so sessions it would start get no tools.
        assert "HW-MCP-004" in listed
        with pytest.raises(SystemExit):
            _run(gateway, "select", "catalog", "find_records")
        assert "TOOL=SHA256" in capsys.readouterr().err
        # An empty selection removes every tool without contacting the server.
        assert _run(gateway, "select", "catalog") == 0
        assert "Enabled for new sessions: none" in capsys.readouterr().out
        assert _run(gateway, "disconnect", "catalog") == 0
        output = capsys.readouterr().out
        assert "No research services configured." in output
        assert "Completed remote work is not undone." in output
        assert gateway.credential_store.resolve("HEARTWOOD_MCP_TOKEN_CATALOG") is None
    finally:
        gateway.stop()


def test_services_refuse_origins_the_deployment_does_not_approve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = _gateway(tmp_path)
    try:
        code = _run(gateway, "add", "other", "https://other.example.org/mcp", "--label", "Other")
        assert code == 1
        assert "HW-MCP-001" in capsys.readouterr().err
    finally:
        gateway.stop()


def test_services_without_a_deployment_allow_no_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    code = main(
        ["services", "add", "catalog", "https://catalog.example.org/mcp", "--label", "Catalog"]
    )
    assert code == 1
    assert "HW-MCP-001" in capsys.readouterr().err


def test_test_output_marks_listing_as_not_authorization() -> None:
    annotations = {"read_only": True, "destructive": False, "idempotent": True, "open_world": False}
    result = api_response(
        McpServerTestResponse,
        {
            "server": {
                "server_id": "catalog",
                "label": "Synthetic catalog",
                "url": "https://catalog.example.org/mcp",
                "origin": "https://catalog.example.org",
                "service_id": "catalog",
                "destination": "allowed",
                "enabled": False,
                "authentication": {"status": "missing", "expires_at": None},
                "tools": [],
                "enabled_tools": [],
            },
            "tools": [
                {
                    "name": "find_records",
                    "description": "Find synthetic records.",
                    "sha256": "a" * 64,
                    "parameters": '{"properties":{"term":{"type":"string"}}}',
                    "annotations": annotations,
                    "allowed": True,
                    "accepts_operation_binding": True,
                    "selected": True,
                    "changed": True,
                }
            ],
            "resources": {"advertised": True, "support": "unsupported"},
            "prompts": {"advertised": False, "support": "unsupported"},
        },
    )
    output = format_mcp_test(result)
    assert "not authorized by listing" in output
    assert f"select as find_records={'a' * 64}" in output
    assert '"term"' in output
    assert "changed since review" in output
    assert "The server advertises resources; Heartwood does not use them." in output
    assert "prompts;" not in output
