# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Review measurement excludes bootstrap work without inventing missing provider usage."""

from pathlib import Path
from uuid import uuid4

import pytest

from heartwood.compliance.research_runner import _TrialSession
from heartwood.gateway import ProjectContext, SessionGateway, SessionProjection
from heartwood.schemas.execution import ExecutionUsage


@pytest.mark.parametrize("accounting", ["known", "unknown", "reset"])
def test_review_usage_subtracts_only_known_bootstrap_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accounting: str
) -> None:
    gateway = SessionGateway(project=ProjectContext(tmp_path), env={})
    session_id = gateway.create_session("Synthetic review measurement")["session_id"]
    try:
        runtime = gateway.evaluation_observation(session_id=session_id)
        baseline = ExecutionUsage(
            input_tokens=100,
            output_tokens=20,
            model_calls=2,
            reported_cost_usd=0.1,
            proposed_actions=0,
            elapsed_seconds=0,
        )
        if accounting == "unknown":
            baseline = baseline.model_copy(
                update={"input_tokens": None, "model_calls": None, "reported_cost_usd": None}
            )
        projection = SessionProjection.model_validate(
            {
                "session_id": session_id,
                "event_count": 0,
                "revision": 0,
                "usage": {
                    "usage_id": "total",
                    "purpose_label": "Agent",
                    "model_name": "synthetic",
                    "call_count": 1 if accounting == "reset" else 6,
                    "prompt_tokens": 350,
                    "completion_tokens": 70,
                    "accumulated_cost": 0.3,
                },
            }
        )
        monkeypatch.setattr("heartwood.compliance.research_runner.time.monotonic", lambda: 110.0)
        session = _TrialSession(
            gateway, session_id, uuid4(), "2026-09-11T00:00:00Z", runtime, usage_baseline=baseline
        )
        if accounting == "reset":
            with pytest.raises(ValueError, match="counters decreased"):
                session.usage(projection, 100.0)
            return
        measured = session.usage(projection, 100.0)
        assert measured.elapsed_seconds == 10.0
        assert measured.output_tokens == 50
        assert measured.proposed_actions == 0
        if accounting == "known":
            assert measured.input_tokens == 250
            assert measured.model_calls == 4
            assert measured.reported_cost_usd == pytest.approx(0.2)
        else:
            assert measured.input_tokens is None
            assert measured.model_calls is None
            assert measured.reported_cost_usd is None
    finally:
        gateway.stop()
