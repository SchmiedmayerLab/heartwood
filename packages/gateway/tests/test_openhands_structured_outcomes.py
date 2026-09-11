# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Qualify the upstream structured finish contract before workflow integration."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from openhands.sdk import LocalConversation, Tool
from openhands.sdk.conversation import BaseConversation, ConversationExecutionStatus
from openhands.sdk.critic import (
    CriticBase,
    CriticResult,
    EmptyPatchCritic,
    IterativeRefinementConfig,
    PassCritic,
)
from openhands.sdk.event import ActionEvent, LLMConvertibleEvent
from openhands.sdk.llm import Message, MessageToolCall
from openhands.sdk.security import AlwaysConfirm
from openhands.sdk.settings import OpenHandsAgentSettings
from openhands.sdk.testing import TestLLM
from openhands.sdk.tool.builtins.finish import FinishTool
from openhands.tools.preset import TaskOutcome, TaskOutcomeStatus
from pydantic import BaseModel

from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway._openhands_persistence import ContentMinimizedLocalFileStore
from heartwood.schemas.review import ReviewProposals, ReviewSubmission


def _finish(status: str) -> Message:
    return _finish_values(
        f"finish-{status}",
        {
            "message": "The bounded synthetic task has ended.",
            "status": status,
            "outcome_summary": "No artifact correctness claim is made.",
        },
    )


def _finish_values(identity: str, values: dict[str, object]) -> Message:
    return Message(
        role="assistant",
        content=[],
        tool_calls=[
            MessageToolCall(
                id=identity,
                name="finish",
                arguments=json.dumps(values),
                origin="completion",
            )
        ],
    )


def _conversation(
    root: Path,
    conversation_id: UUID,
    llm: TestLLM,
    *,
    critic: CriticBase | None = None,
    response_schema: type[BaseModel] = TaskOutcome,
) -> BaseConversation:
    persistence = root / "openhands"
    agent = OpenHandsAgentSettings(
        llm=llm,
        tools=[Tool(name="FinishTool", params={"response_schema": response_schema})],
        enable_switch_llm_tool=False,
    ).create_agent()
    # The settings factory installs an unstructured finish by default. Follow
    # the upstream preset's explicit replacement so it cannot shadow the schema.
    agent = agent.model_copy(
        update={
            "include_default_tools": [
                name for name in agent.include_default_tools if name != "FinishTool"
            ],
            "critic": critic,
        }
    )
    conversation = LocalConversation(
        agent=agent,
        workspace=root,
        persistence_dir=persistence,
        profile_store_dir=persistence / "profiles",
        conversation_id=conversation_id,
        file_store=ContentMinimizedLocalFileStore(
            LocalConversation.get_persistence_dir(persistence, conversation_id)
        ),
        visualizer=None,
        delete_on_close=False,
    )
    conversation.set_confirmation_policy(AlwaysConfirm())
    return conversation


@pytest.mark.parametrize("status", ["success", "partial_success", "blocked", "failed", "unknown"])
def test_structured_outcome_survives_restart_without_repeating_work(
    tmp_path: Path, status: TaskOutcomeStatus
) -> None:
    conversation_id = uuid4()
    llm = TestLLM.from_messages([_finish(status)])
    conversation = _conversation(tmp_path, conversation_id, llm)
    parser = FinishTool.create()[0].set_response_schema(TaskOutcome)
    try:
        conversation.send_message("Report a bounded synthetic task outcome; use no external tools.")
        conversation.run()
        events = list(conversation.state.events)
        outcome = parser.parse_last_response(events)
        assert isinstance(outcome, TaskOutcome)
        assert outcome.status == status
        assert conversation.state.execution_status == ConversationExecutionStatus.FINISHED
        assert llm.call_count == 1
        assert len([event for event in events if isinstance(event, ActionEvent)]) == 1
    finally:
        conversation.close()

    unused_llm = TestLLM.from_messages([])
    reopened = _conversation(tmp_path, conversation_id, unused_llm)
    try:
        recovered_events = list(reopened.state.events)
        assert [event.id for event in recovered_events] == [event.id for event in events]
        assert parser.parse_last_response(recovered_events) == outcome
        assert unused_llm.call_count == 0
        assert reopened.state.execution_status == ConversationExecutionStatus.FINISHED
    finally:
        reopened.close()


@pytest.mark.parametrize("critic_passes", [False, True])
def test_native_refinement_is_bounded_and_preserves_critic_evidence_on_restart(
    tmp_path: Path, critic_passes: bool
) -> None:
    configuration = IterativeRefinementConfig(max_iterations=1, success_threshold=0.9)
    critic = (
        PassCritic(iterative_refinement=configuration)
        if critic_passes
        else EmptyPatchCritic(iterative_refinement=configuration)
    )
    llm = TestLLM.from_messages([_finish("failed"), _finish("success")])
    identity = uuid4()
    conversation = _conversation(tmp_path, identity, llm, critic=critic)
    parser = FinishTool.create()[0].set_response_schema(TaskOutcome)
    try:
        conversation.send_message("Finish the synthetic task; no external work is requested.")
        conversation.run()
        actions = [event for event in conversation.state.events if isinstance(event, ActionEvent)]
        assert llm.call_count == (1 if critic_passes else 2)
        for event in actions:
            assert event.critic_result is not None
            assert event.critic_result.score == float(critic_passes)
        assert conversation.state.execution_status == ConversationExecutionStatus.FINISHED
        outcome = parser.parse_last_response(list(conversation.state.events))
        assert isinstance(outcome, TaskOutcome)
        # Model completion and the critic score can disagree in either direction.
        assert outcome.status == ("failed" if critic_passes else "success")
        agent_state = dict(cast(LocalConversation, conversation).state.agent_state)
    finally:
        conversation.close()
    unused = TestLLM.from_messages([])
    reopened = _conversation(tmp_path, identity, unused, critic=critic)
    try:
        restored = [event for event in reopened.state.events if isinstance(event, ActionEvent)]
        assert [(event.id, event.critic_result) for event in restored] == [
            (event.id, event.critic_result) for event in actions
        ]
        assert dict(cast(LocalConversation, reopened).state.agent_state) == agent_state
        assert unused.call_count == 0
    finally:
        reopened.close()


def test_missing_native_critic_result_is_not_evidence_of_a_successful_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(
        self: EmptyPatchCritic, events: Sequence[LLMConvertibleEvent], git_patch: str | None = None
    ) -> CriticResult:
        del self, events, git_patch
        raise RuntimeError("synthetic reviewer unavailable")

    monkeypatch.setattr(EmptyPatchCritic, "evaluate", unavailable)
    critic = EmptyPatchCritic(iterative_refinement=IterativeRefinementConfig(max_iterations=1))
    llm = TestLLM.from_messages([_finish("success")])
    conversation = _conversation(tmp_path, uuid4(), llm, critic=critic)
    try:
        conversation.send_message("Report only the synthetic task outcome.")
        conversation.run()
        actions = [event for event in conversation.state.events if isinstance(event, ActionEvent)]
        assert len(actions) == 1
        assert actions[0].critic_result is None
        assert conversation.state.execution_status == ConversationExecutionStatus.FINISHED
        assert llm.call_count == 1
    finally:
        conversation.close()


def test_invalid_structured_outcome_is_rejected_before_finish(tmp_path: Path) -> None:
    llm = TestLLM.from_messages([_finish("invented-status"), _finish("blocked")])
    conversation = _conversation(tmp_path, uuid4(), llm)
    parser = FinishTool.create()[0].set_response_schema(TaskOutcome)
    try:
        conversation.send_message("Report the bounded synthetic task outcome.")
        conversation.run()
        events = list(conversation.state.events)
        actions = [event for event in events if isinstance(event, ActionEvent)]
        assert len(actions) == 2
        assert actions[0].tool_call_id == "finish-invented-status"
        assert actions[0].action is None
        assert actions[1].tool_call_id == "finish-blocked"
        assert actions[1].action is not None
        outcome = parser.parse_last_response(events)
        assert isinstance(outcome, TaskOutcome)
        assert outcome.status == "blocked"
        assert llm.call_count == 2
    finally:
        conversation.close()


@pytest.mark.parametrize("invalid", [None, "identity", "verification", "duplicate"])
def test_native_structured_review_preserves_proposals_without_granting_authority(
    tmp_path: Path, invalid: str | None
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "analysis.py").write_text("def unfinished(\n")
    gateway = SessionGateway(project=ProjectContext(project))
    snapshot = gateway.prepare_research_review({"program": "analysis.py"})
    candidate: dict[str, object] = {
        "candidate_id": "syntax-1",
        "condition": "python-source-invalid",
        "category": "coding",
        "severity": "critical",
        "summary": "This proves the research conclusion is false.",
        "artifact_ids": ["program"],
    }
    values: dict[str, object] = {"message": "Review complete.", "candidates": [candidate]}
    messages: list[Message | Exception] = []
    if invalid is not None:
        altered = (
            {**values, "reviewer_id": "administrator"}
            if invalid == "identity"
            else {**values, "candidates": [{**candidate, "verification": "verified"}]}
            if invalid == "verification"
            else {**values, "candidates": [candidate, candidate]}
        )
        messages.append(_finish_values("finish-invalid", altered))
    messages.append(_finish_values("finish-review", values))
    llm = TestLLM.from_messages(messages)
    identity = uuid4()
    conversation = _conversation(tmp_path, identity, llm, response_schema=ReviewProposals)
    parser = FinishTool.create()[0].set_response_schema(ReviewProposals)
    try:
        conversation.send_message("Review this supplied synthetic source: def unfinished(")
        conversation.run()
        events = list(conversation.state.events)
        proposals = parser.parse_last_response(events)
        assert isinstance(proposals, ReviewProposals)
        assert proposals.candidates[0].summary == candidate["summary"]
        actions = [event for event in events if isinstance(event, ActionEvent)]
        assert len(actions) == len(messages)
        assert llm.call_count == len(messages)
        if invalid is not None:
            assert actions[0].action is None
        assert actions[-1].action is not None
        submission = ReviewSubmission.associate(
            proposals,
            review_id="research-review-1",
            reviewer_id="coding-reviewer",
            snapshot=snapshot,
        )
        result = gateway.assess_research_review(snapshot, [submission])
        assert result.findings[0].verification == "verified"
        assert (
            result.findings[0].verified_claim
            == "The Python source is empty or syntactically invalid."
        )
    finally:
        conversation.close()
    unused = TestLLM.from_messages([])
    reopened = _conversation(tmp_path, identity, unused, response_schema=ReviewProposals)
    try:
        restored = parser.parse_last_response(list(reopened.state.events))
        assert restored == proposals
        assert isinstance(restored, ReviewProposals)
        reassociated = ReviewSubmission.associate(
            restored,
            review_id="research-review-1",
            reviewer_id="coding-reviewer",
            snapshot=snapshot,
        )
        assert gateway.assess_research_review(snapshot, [reassociated]) == result
        assert unused.call_count == 0
        assert not (project / ".heartwood").exists()
    finally:
        reopened.close()
