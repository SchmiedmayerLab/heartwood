# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for synthetic reviewer packet generation."""

from __future__ import annotations

from pathlib import Path

from heartwood.compliance import ReviewerPacketGenerator
from heartwood.gateway import SessionGateway
from heartwood.session import CommandKind, JsonValue, SessionCommand


def test_reviewer_packet_uses_synthetic_fixtures_and_scrubbed_audit(tmp_path: Path) -> None:
    workspace = tmp_path / "sessions"
    session_id = "review-session"
    gateway = SessionGateway(workspace=workspace)
    gateway.handle(_command(session_id, CommandKind.DETECT))
    gateway.handle(
        _command(
            session_id,
            CommandKind.RUN,
            prompt="participant-level prompt must not appear",
            endpoint="https://public.example.invalid/v1/chat/completions",
        )
    )
    gateway.handle(_command(session_id, CommandKind.AUDIT_EXPORT))

    packet = ReviewerPacketGenerator(
        repository_root=_repo_root(),
        session_workspace=workspace,
        session_id=session_id,
        fixture_root=_repo_root() / "fixtures" / "synthetic",
        output_dir=tmp_path / "packet",
    ).generate()

    assert packet.index_path.is_file()
    assert {path.name for path in packet.files} == {
        "reviewer-packet.md",
        "policy-profile.json",
        "egress-attestation.json",
        "sample-audit.jsonl",
        "dependency-license-summary.md",
        "current-limitations.md",
    }
    packet_text = packet.index_path.read_text(encoding="utf-8")
    audit_text = (packet.output_dir / "sample-audit.jsonl").read_text(encoding="utf-8")
    limitations_text = (packet.output_dir / "current-limitations.md").read_text(encoding="utf-8")
    assert "Synthetic Reviewer Packet" in packet_text
    assert "Data-Flow Diagram" in packet_text
    assert "participant-level prompt must not appear" not in audit_text
    assert "[scrubbed]" in audit_text
    assert "local synthetic smoke artifact" in limitations_text
    assert "bounded bash execution" in limitations_text
    assert "larger local model" in limitations_text
    assert "llama-cpp-cpu" in limitations_text
    assert "next documentation pass" in limitations_text
    assert "Jupyter-style proxy smoke" in limitations_text
    assert "live controlled-platform validation remains future work" in limitations_text
    assert "planned after the documentation-site pass" not in limitations_text


def test_reviewer_packet_can_fall_back_to_checked_in_audit_fixture(tmp_path: Path) -> None:
    packet = ReviewerPacketGenerator(
        repository_root=_repo_root(),
        session_workspace=tmp_path / "empty-sessions",
        session_id="missing-session",
        fixture_root=_repo_root() / "fixtures" / "synthetic",
        output_dir=tmp_path / "packet",
    ).generate()

    audit_text = (packet.output_dir / "sample-audit.jsonl").read_text(encoding="utf-8")

    assert "heartwood.audit-event.v1" in audit_text
    assert "Synthetic Reviewer Packet" in packet.index_path.read_text(encoding="utf-8")


def _command(session_id: str, kind: CommandKind, **payload: JsonValue) -> SessionCommand:
    return SessionCommand(
        command_id=f"{session_id}-{kind.value}",
        session_id=session_id,
        kind=kind,
        actor_id="synthetic-user",
        created_at="2026-01-01T00:00:00Z",
        payload=payload,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]
