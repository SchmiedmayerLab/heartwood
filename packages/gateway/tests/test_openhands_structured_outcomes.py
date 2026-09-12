# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Qualify the upstream structured finish contract before workflow integration."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from openhands.sdk import LocalConversation, Tool
from openhands.sdk.conversation import BaseConversation, ConversationExecutionStatus
from openhands.sdk.event import ActionEvent
from openhands.sdk.llm import Message, MessageToolCall
from openhands.sdk.security import AlwaysConfirm
from openhands.sdk.settings import OpenHandsAgentSettings
from openhands.sdk.testing import TestLLM
from openhands.sdk.tool.builtins.finish import FinishTool
from openhands.tools.preset import TaskOutcome, TaskOutcomeStatus

from heartwood.gateway._openhands_persistence import ContentMinimizedLocalFileStore


def _finish(status: str) -> Message:
    return Message(
        role="assistant",
        content=[],
        tool_calls=[
            MessageToolCall(
                id=f"finish-{status}",
                name="finish",
                arguments=json.dumps(
                    {
                        "message": "The bounded synthetic task has ended.",
                        "status": status,
                        "outcome_summary": "No artifact correctness claim is made.",
                    }
                ),
                origin="completion",
            )
        ],
    )


def _conversation(root: Path, conversation_id: UUID, llm: TestLLM) -> BaseConversation:
    persistence = root / "openhands"
    agent = OpenHandsAgentSettings(
        llm=llm,
        tools=[Tool(name="FinishTool", params={"response_schema": TaskOutcome})],
        enable_switch_llm_tool=False,
    ).create_agent()
    # The settings factory installs an unstructured finish by default. Follow
    # the upstream preset's explicit replacement so it cannot shadow the schema.
    agent = agent.model_copy(
        update={
            "include_default_tools": [
                name for name in agent.include_default_tools if name != "FinishTool"
            ]
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
