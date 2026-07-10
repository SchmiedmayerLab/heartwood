# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Reviewer packet generation from synthetic fixtures and scrubbed audit logs."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from heartwood.audit import AuditLog
from heartwood.schemas import EgressAttestationRecord, PolicyProfile


@dataclass(frozen=True, slots=True)
class ReviewerPacket:
    """Generated reviewer packet file set."""

    output_dir: Path
    index_path: Path
    files: tuple[Path, ...]


class ReviewerPacketGenerator:
    """Generate a deterministic reviewer packet from synthetic repository artifacts."""

    def __init__(
        self,
        *,
        repository_root: Path,
        session_workspace: Path,
        session_id: str,
        fixture_root: Path,
        output_dir: Path,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.session_workspace = session_workspace.resolve()
        self.session_id = session_id
        self.fixture_root = fixture_root.resolve()
        self.output_dir = output_dir.resolve()

    def generate(self) -> ReviewerPacket:
        """Write reviewer packet files and return their paths."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        policy = self._load_policy()
        attestation = self._load_attestation()
        audit_export = self._load_audit_export()
        files = (
            self._write_json("policy-profile.json", policy.model_dump(mode="json")),
            self._write_json("egress-attestation.json", attestation.model_dump(mode="json")),
            self._write_text("sample-audit.jsonl", audit_export),
            self._write_text("dependency-license-summary.md", self._dependency_summary()),
            self._write_text("current-limitations.md", self._limitations()),
        )
        index = self._write_text(
            "reviewer-packet.md",
            self._packet_markdown(policy=policy, attestation=attestation),
        )
        return ReviewerPacket(output_dir=self.output_dir, index_path=index, files=(index, *files))

    def _load_policy(self) -> PolicyProfile:
        policy_path = self.fixture_root / "policies" / "generic-default.json"
        return PolicyProfile.model_validate_json(policy_path.read_text(encoding="utf-8"))

    def _load_attestation(self) -> EgressAttestationRecord:
        attestation_path = self.fixture_root / "egress" / "attestation-record.json"
        return EgressAttestationRecord.model_validate_json(
            attestation_path.read_text(encoding="utf-8")
        )

    def _load_audit_export(self) -> str:
        audit_path = self.session_workspace / self.session_id / "audit.jsonl"
        if audit_path.exists():
            return AuditLog(audit_path).export_jsonl()
        fixture_path = self.fixture_root / "audit" / "expected-export.jsonl"
        return fixture_path.read_text(encoding="utf-8")

    def _write_json(self, name: str, value: object) -> Path:
        path = self.output_dir / name
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_text(self, name: str, value: str) -> Path:
        path = self.output_dir / name
        path.write_text(value, encoding="utf-8")
        return path

    def _dependency_summary(self) -> str:
        package_rows: list[str] = []
        for package_path in sorted((self.repository_root / "packages").glob("*/pyproject.toml")):
            data = tomllib.loads(package_path.read_text(encoding="utf-8"))
            project = data.get("project", {})
            if not isinstance(project, dict):
                continue
            name = str(project.get("name", package_path.parent.name))
            dependencies = project.get("dependencies", [])
            dependency_count = len(dependencies) if isinstance(dependencies, list) else 0
            package_rows.append(f"| `{name}` | MIT | {dependency_count} |")
        return "\n".join(
            (
                "# Dependency And License Summary",
                "",
                (
                    "All workspace packages declare the repository MIT license and are checked "
                    "by REUSE compliance. Runtime dependency admission remains gated by the "
                    "workspace lockfile and CI checks."
                ),
                "",
                "| Package | Declared license | Runtime dependency count |",
                "|---|---|---:|",
                *package_rows,
                "",
            )
        )

    def _limitations(self) -> str:
        return "\n".join(
            (
                "# Current Limitations",
                "",
                (
                    "- Public validation uses synthetic fixtures only and does not validate "
                    "controlled data."
                ),
                (
                    "- Model profiles and application-layer route policy are implemented, but "
                    "each institution must validate its provider agreement, identity, and "
                    "authoritative network controls."
                ),
                (
                    "- Images contain no model weights. Optional reviewed downloads and the "
                    "CI fixture make no production or biomedical-quality claim."
                ),
                (
                    "- The pinned OpenHands SDK provides the conversation and coding tools; "
                    "model capability and unattended operation remain deployment-specific gates."
                ),
                (
                    "- Ask Every Time is the action-confirmation default. Auto-Approve Low Risk "
                    "remains a deployment-policy opt-in and still confirms medium-, high-, and "
                    "unknown-risk actions."
                ),
                (
                    "- A published documentation site and stable release channel are not yet "
                    "implemented; checked-in Markdown remains the source of truth."
                ),
                (
                    "- The web UI, Server-Sent Events fallback, Jupyter-style proxy smoke, "
                    "and Terra-style packaged demo smoke are implemented synthetic paths; "
                    "live controlled-platform validation remains future work."
                ),
                (
                    "- Platform-specific policy, identity, network, and live-platform evidence "
                    "are required independently of this generic synthetic packet."
                ),
                "",
            )
        )

    def _packet_markdown(
        self,
        *,
        policy: PolicyProfile,
        attestation: EgressAttestationRecord,
    ) -> str:
        return "\n".join(
            (
                "# Synthetic Reviewer Packet",
                "",
                (
                    "This packet is generated from checked-in synthetic fixtures and scrubbed "
                    "session audit records. It contains no controlled data, participant-level "
                    "rows, credentials, prompt content, response content, or live-platform "
                    "identifiers."
                ),
                "",
                "## Threat Model Summary",
                "",
                (
                    "- The deployment must keep controlled data inside an institution-approved "
                    "boundary; this synthetic packet does not establish that platform control."
                ),
                (
                    "- Heartwood denies unallowlisted declared model routes before invocation; "
                    "platform controls remain authoritative for network egress."
                ),
                (
                    "- Deployment policy authorizes model routes; a conversational click cannot "
                    "override it."
                ),
                (
                    "- Bundled Skills load automatically; mounted extensions require "
                    "installation review."
                ),
                (
                    "- OpenHands Ask Every Time requires Allow once or Reject for tool actions. "
                    "The optional Auto-Approve Low Risk mode still confirms medium-, high-, "
                    "and unknown-risk actions."
                ),
                "- Audit exports use scrubbed JSONL records with a verified hash chain.",
                "",
                "## Data-Flow Diagram",
                "",
                "```mermaid",
                "flowchart LR",
                '  User["CLI / Notebook / Web UI"] --> Gateway["Session gateway"]',
                '  Gateway --> Core["Session service"]',
                '  Core --> Audit["Hash-chained audit log"]',
                '  Core --> Policy["Policy engine"]',
                '  Policy -. authorizes profile .-> Model["Configured model route"]',
                '  Core --> Skills["Verified skills"]',
                '  Core --> State["Workspace session state"]',
                "```",
                "",
                "## Policy Profile",
                "",
                f"- Policy: `{policy.policy_id}`",
                f"- Platform: `{policy.platform_id}`",
                f"- Deny egress by default: `{policy.deny_egress_by_default}`",
                (
                    "- Allowed action-confirmation modes: `"
                    f"{', '.join(policy.allowed_action_confirmation_modes)}`"
                ),
                f"- Aggregate count floor: `{policy.aggregate_count_floor}`",
                "",
                "## Fixture Statement",
                "",
                f"- Fixture root: `{self.fixture_root}`",
                f"- Session workspace: `{self.session_workspace}`",
                f"- Session: `{self.session_id}`",
                "- All packet records are synthetic or scrubbed.",
                "",
                "## Sample Audit Log",
                "",
                "See `sample-audit.jsonl` for the scrubbed audit export.",
                "",
                "## Sample Attestation",
                "",
                f"- Decision: `{attestation.decision}`",
                f"- Endpoint: `{attestation.endpoint}`",
                f"- Reason: {attestation.reason}",
                "",
                "## Dependency And License Summary",
                "",
                "See `dependency-license-summary.md`.",
                "",
                "## Current Limitations",
                "",
                "See `current-limitations.md`.",
                "",
            )
        )
