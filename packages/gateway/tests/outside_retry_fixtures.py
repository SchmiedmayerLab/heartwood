# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""A gateway holding an outside review whose approval was recorded while records were offline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import AnyHttpUrl

from heartwood.audit import LocalEd25519CheckpointSigner, checkpoint_public_key_fingerprint
from heartwood.audit._anchored_signer import AnchoredCheckpointSigner
from heartwood.audit._records_s3 import CheckpointAuthorityScope, S3CheckpointRecords
from heartwood.core_adapter import (
    BackendEventSink,
    BackendToolCallEvent,
    DeterministicAgentBackend,
    ProposedToolCall,
    SessionService,
    TokenDeltaSink,
)
from heartwood.core_adapter.checkpoint_evidence import SessionCheckpointEvidence
from heartwood.core_adapter.external_actions import (
    ExternalActionDecision,
    ExternalActionObservation,
    ExternalActionProposal,
    ExternalActionRecord,
    ExternalActionRequest,
    ExternalInputReference,
    canonical_request,
    external_action_records,
)
from heartwood.core_adapter.worker_attempts import WorkerControlPlane, WorkerRuntimeBinding
from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.schemas import AuditRetention, JsonValue, PlatformOperationObservation
from heartwood.session import CommandKind, SessionCommand
from packages.audit.tests.records_fixtures import MemoryS3

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SESSION = "session-outside"
_IMAGE = "sha256:" + "a" * 64
_INPUT = b"synthetic input"


class _Worker(DeterministicAgentBackend):
    def bind_runtime(self, *, event_sink: BackendEventSink, token_sink: TokenDeltaSink) -> None:
        super().bind_runtime(event_sink=event_sink, token_sink=token_sink)
        self.emit = event_sink


class OutsideService:
    """Trusted outside service that counts every submission."""

    tool_names = frozenset({"submit_analysis"})
    policy_sha256 = "e" * 64

    def __init__(self) -> None:
        self.submissions = 0

    def prepare(self, tool_name: str, arguments: dict[str, JsonValue]) -> ExternalActionRequest:
        assert arguments == {"analysis": "synthetic"}
        return ExternalActionRequest(
            service_id="synthetic-analysis",
            tool_name=tool_name,
            destination="synthetic-results",
            summary="Submit a synthetic analysis",
            request_json=canonical_request('{"analysis":"synthetic"}'),
            inputs=(
                ExternalInputReference(
                    reference="synthetic-input",
                    size_bytes=len(_INPUT),
                    sha256=hashlib.sha256(_INPUT).hexdigest(),
                ),
            ),
        )

    def check_policy(self, proposal: ExternalActionProposal, actor_id: str, now: datetime) -> None:
        assert actor_id in {"human", "researcher"}
        assert proposal.created_at <= now < proposal.expires_at

    def read_input(self, reference: ExternalInputReference) -> bytes:
        assert reference.reference == "synthetic-input"
        return _INPUT

    def submit(
        self,
        proposal: ExternalActionProposal,
        decision: ExternalActionDecision,
        inputs: tuple[bytes, ...],
    ) -> ExternalActionObservation:
        assert decision.proposal_sha256 == proposal.sha256
        assert inputs == (_INPUT,)
        self.submissions += 1
        return self.observe(proposal)

    def observe(self, proposal: ExternalActionProposal) -> ExternalActionObservation:
        return ExternalActionObservation(
            proposal_sha256=proposal.sha256,
            operation=PlatformOperationObservation(
                project_context_id=str(proposal.worker.project_id),
                operation_id=str(proposal.operation_id),
                service_id=proposal.request.service_id,
                backend_operation_ref="synthetic-operation",
                observed_at=NOW,
                status="completed",
                platform_record_ref="synthetic-audit",
            ),
        )

    def prepare_result(self, _record: ExternalActionRecord) -> str:
        raise AssertionError("no result is released in this test")

    def check_result_policy(self, *_args: object) -> None:
        raise AssertionError("no result is released in this test")


def _evidence(tmp_path: Path, project_id: object) -> tuple[SessionCheckpointEvidence, MemoryS3]:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "synthetic-signer.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    client = MemoryS3()
    records = S3CheckpointRecords(
        client=client,
        bucket="synthetic-records",
        prefix="audit",
        scope=CheckpointAuthorityScope(
            "synthetic",
            project_id,  # type: ignore[arg-type]
            SESSION,
            AuditRetention(policy_id="synthetic", retain_until="2033-01-01"),
        ),
    )
    trusted = {checkpoint_public_key_fingerprint(key.public_key()): key.public_key()}
    signer = LocalEd25519CheckpointSigner(
        private_key=path, signer_id="synthetic", key_id="test", key_version="1"
    )
    authority = AnchoredCheckpointSigner(
        records=records, signer=signer, trusted_keys=trusted, clock=lambda: NOW
    )
    controller = SessionCheckpointEvidence(
        records=records, signer=authority, trusted_keys=trusted, mode="buffered"
    )
    return controller, client


@dataclass
class OutsideReview:
    """A gateway session with one outside review awaiting the researcher."""

    gateway: SessionGateway
    service: SessionService
    outside: OutsideService
    records: MemoryS3
    evidence: SessionCheckpointEvidence
    group_id: str
    operation_id: UUID

    def phase(self) -> str:
        """Return the outside operation's recorded phase."""
        return external_action_records(self.service.replay_events())[self.operation_id].phase

    def expire(self) -> None:
        """Move the session clock past the review's validity."""
        self.service.clock = lambda: (NOW + timedelta(minutes=5)).isoformat()

    def decision(self, *, approved: bool = True) -> SessionCommand:
        """Return a new researcher decision for the review."""
        return SessionCommand(
            session_id=SESSION,
            command_id=str(uuid4()),
            kind=CommandKind.APPROVE if approved else CommandKind.DENY,
            actor_id="researcher",
            created_at=NOW.isoformat(),
            payload={"target_type": "action-set", "target_id": self.group_id},
        )


@contextmanager
def outside_review(tmp_path: Path) -> Iterator[OutsideReview]:
    """Open a gateway whose session holds one proposed outside operation."""
    project_id = uuid4()
    outside = OutsideService()
    controller, client = _evidence(tmp_path, project_id)

    def service_factory(root: Path, session_id: str) -> SessionService:
        service = SessionService.local_default(
            root,
            session_id=session_id,
            backend=_Worker(),
            env={},
            clock=lambda: NOW.isoformat(),
            worker_project_id=project_id,
            external_actions=outside,
            checkpoint_evidence=controller,
        )
        intent = service.prepare_worker_attempt(
            image_digest=_IMAGE,
            sandbox_policy_sha256="b" * 64,
            input_manifest_sha256="c" * 64,
            control_plane=WorkerControlPlane(
                control_plane_sha256="1" * 64, container_runtime_sha256="2" * 64
            ),
        )
        service.bind_worker_runtime(
            attempt_id=intent.attempt_id,
            runtime=WorkerRuntimeBinding(
                sandbox_id="synthetic-worker",
                generation="synthetic-generation",
                endpoint=AnyHttpUrl("http://worker.test"),
                image_digest=_IMAGE,
                sandbox_policy_sha256="b" * 64,
            ),
        )
        return service

    workspace = tmp_path / "project"
    workspace.mkdir()
    gateway = SessionGateway(
        project=ProjectContext(
            workspace,
            protected_state=tmp_path / "protected" / str(project_id),
            project_id=project_id,
        ),
        env={},
        backend_id="deterministic",
        service_factory=service_factory,
    )
    try:
        with gateway._state_lock:
            gateway._project_activity.initialize()
            service = gateway._service(SESSION)
        backend = service.backend
        assert isinstance(backend, _Worker)
        backend.emit(
            (
                BackendToolCallEvent(
                    tool_call=ProposedToolCall(
                        tool_call_id="worker-call",
                        tool_name="submit_analysis",
                        risk="unknown",
                        summary="Worker proposal",
                        arguments={"analysis": "synthetic"},
                    ),
                    source_event_id="worker-proposal",
                ),
            )
        )
        proposed = service.propose_external_action(service.replay_events()[-1].event_id)
        record = ExternalActionRecord.model_validate_json(json.dumps(proposed.payload["record"]))
        yield OutsideReview(
            gateway=gateway,
            service=service,
            outside=outside,
            records=client,
            evidence=controller,
            group_id=record.proposal.group_id,
            operation_id=record.proposal.operation_id,
        )
    finally:
        gateway.stop()
