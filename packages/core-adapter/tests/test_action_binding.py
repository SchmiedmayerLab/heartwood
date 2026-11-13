# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Content binding for complete action-set review."""

from __future__ import annotations

from dataclasses import replace

import pytest

from heartwood.core_adapter import ProposedToolCall, pending_action_group


def _action() -> ProposedToolCall:
    return ProposedToolCall(
        tool_call_id="call-1",
        action_id="action-1",
        tool_name="terminal",
        kind="terminal",
        risk="unknown",
        summary="Create synthetic output",
        arguments={"command": "printf reviewed > result.txt", "timeout": 10},
    )


def test_action_binding_is_full_length_and_canonical() -> None:
    action = _action()
    original = pending_action_group((action,))
    reordered = pending_action_group(
        (replace(action, arguments={"timeout": 10, "command": "printf reviewed > result.txt"}),)
    )
    assert original is not None
    assert reordered is not None
    assert original.group_id == reordered.group_id
    digest = original.group_id.removeprefix("action-set-")
    assert len(digest) == 64
    assert int(digest, 16) >= 0
    assert pending_action_group(()) is None


@pytest.mark.parametrize(
    "changed",
    [
        replace(_action(), tool_call_id="another-call"),
        replace(_action(), action_id="another-action"),
        replace(_action(), tool_name="another-tool"),
        replace(_action(), kind="other"),
        replace(_action(), arguments={"command": "printf changed > result.txt", "timeout": 10}),
        replace(_action(), arguments={"command": "printf reviewed > result.txt", "timeout": 20}),
        replace(
            _action(),
            arguments={
                "command": "printf reviewed > result.txt",
                "timeout": 10,
                "extra": {"destination": "another-place"},
            },
        ),
    ],
)
def test_execution_changes_invalidate_review(changed: ProposedToolCall) -> None:
    original = pending_action_group((_action(),))
    current = pending_action_group((changed,))
    assert original is not None
    assert current is not None
    assert original.group_id != current.group_id


def test_action_order_membership_and_nested_lists_are_bound() -> None:
    first = _action()
    second = replace(first, tool_call_id="call-2", action_id="action-2")
    original = pending_action_group((first, second))
    reordered = pending_action_group((second, first))
    shortened = pending_action_group((first,))
    assert original is not None
    assert reordered is not None
    assert shortened is not None
    assert len({original.group_id, reordered.group_id, shortened.group_id}) == 3
    left = pending_action_group((replace(first, arguments={"inputs": ["a", "b"]}),))
    right = pending_action_group((replace(first, arguments={"inputs": ["b", "a"]}),))
    assert left is not None
    assert right is not None
    assert left.group_id != right.group_id


def test_advisory_presentation_does_not_grant_execution_authority() -> None:
    original = pending_action_group((_action(),))
    updated = pending_action_group((replace(_action(), risk="high", summary="Another label"),))
    assert original is not None
    assert updated is not None
    assert original.group_id == updated.group_id


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_noncanonical_numbers_cannot_receive_an_approval_binding(value: float) -> None:
    with pytest.raises(ValueError, match="JSON"):
        pending_action_group((replace(_action(), arguments={"value": value}),))
