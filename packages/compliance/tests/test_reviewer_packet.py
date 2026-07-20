# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for synthetic reviewer packet generation."""

from __future__ import annotations

from pathlib import Path

from heartwood.compliance import ReviewerPacketGenerator
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.session import CommandKind, JsonValue, SessionCommand


def test_reviewer_packet_uses_synthetic_fixtures_and_scrubbed_audit(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    workspace = project.sessions_dir
    session_id = "review-session"
    gateway = SessionGateway(
        project=project,
        env={},
        backend_id="deterministic",
    )
    gateway.handle(
        _command(
            session_id,
            CommandKind.CHAT,
            prompt="participant-level prompt must not appear",
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
    assert "Auto-Approve Low Risk" in packet_text
    assert "platform controls remain authoritative for network egress" in packet_text
    assert "In-boundary model endpoint" not in packet_text
    assert "Allowed action-confirmation modes: `always-confirm, confirm-risky`" in packet_text
    assert "participant-level prompt must not appear" not in audit_text
    assert "[scrubbed]" in audit_text
    assert "Images contain no model weights" in limitations_text
    assert "pinned OpenHands SDK" in limitations_text
    assert "provider agreement" in limitations_text
    assert "application-layer route policy" in limitations_text
    assert "unattended operation" in limitations_text
    assert "Ask Every Time is the action-confirmation default" in limitations_text
    assert "published documentation site" in limitations_text.lower()
    assert "Jupyter-style proxy smoke" in limitations_text
    assert "live controlled-platform validation remains future work" in limitations_text
    assert "Platform-specific policy, identity, network" in limitations_text


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
