# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Read-only fresh-process verification of a synthetic benchmark session."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.session import compute_session_event_hash


def replay_evidence(gateway: SessionGateway, session_id: str) -> dict[str, object]:
    """Fingerprint authoritative replay and audit without initializing an agent."""
    events = gateway.replay_events(session_id=session_id)
    projection = gateway.persisted_session_projection(session_id=session_id).safe_dict()
    for key in ("streamEpoch", "streamRevision"):
        projection.pop(key, None)
    serialized = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    return {
        "event_count": len(events),
        "terminal_event_hash": compute_session_event_hash(events[-1]) if events else None,
        "projection_sha256": hashlib.sha256(serialized).hexdigest(),
        "audit": asdict(gateway.verify_audit(session_id)),
    }


def main() -> int:
    """Emit only replay digests; never send a command or create a model client."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("session_id")
    arguments = parser.parse_args()
    gateway = SessionGateway(project=ProjectContext(arguments.project), env={})
    try:
        print(json.dumps(replay_evidence(gateway, arguments.session_id), sort_keys=True))
    finally:
        gateway.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
