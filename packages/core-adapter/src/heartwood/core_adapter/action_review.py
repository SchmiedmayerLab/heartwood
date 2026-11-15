# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Decide which pending action groups the gateway may allow without the researcher.

The agent loop always pauses before every action group. These pure rules read only
the gateway's typed classification of each action, the review mode, and whether the
session runs in an admitted sandbox worker. Model-written summaries and risk hints
are not inputs. Every unrecognized or ambiguous input selects researcher review.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final, get_args

from heartwood.core_adapter._facade import PendingActionGroup, ReviewClass
from heartwood.schemas import ActionReviewMode

AUTOMATIC_REVIEW_ACTOR: Final = "gateway"
"""Actor recorded for decisions the gateway makes; no interface command may claim it."""

REVIEW_MODES: Final[tuple[ActionReviewMode, ...]] = get_args(ActionReviewMode.__value__)
"""Every review mode, strictest first."""

_PROJECT_RULES: Final[dict[ReviewClass, str]] = {
    "read": "read.file-view",
    "plan": "plan.task-tracker",
}
_CONTAINED_RULE: Final = "sandbox.contained"
_CONTAINED_CLASSES: Final[frozenset[ReviewClass]] = frozenset({"read", "plan", "workspace"})


@dataclass(frozen=True, slots=True)
class AutomaticReview:
    """The mode and rule that allow one complete action group."""

    mode: ActionReviewMode
    rule: str


def default_review_mode(*, sandboxed: bool) -> ActionReviewMode:
    """Return the mode used when the researcher has not selected one."""
    return "review-outside-effects" if sandboxed else "review-all"


def is_tightening(current: ActionReviewMode, selected: ActionReviewMode) -> bool:
    """Return whether a selection asks the researcher at least as often as before."""
    return REVIEW_MODES.index(selected) <= REVIEW_MODES.index(current)


def effective_review_mode(
    selected: str | None,
    *,
    allowed: Collection[str],
    sandboxed: bool,
) -> ActionReviewMode:
    """Apply the policy ceiling and execution boundary to a selection.

    A mode the policy does not list, a mode that needs a sandbox the session does
    not have, and any unknown value all resolve to complete researcher review.
    """
    requested = default_review_mode(sandboxed=sandboxed) if selected is None else selected
    for mode in REVIEW_MODES:
        if mode == requested and mode in allowed:
            if mode == "review-outside-effects" and not sandboxed:
                break
            return mode
    return "review-all"


def automatic_review(
    group: PendingActionGroup,
    *,
    mode: ActionReviewMode,
    sandboxed: bool,
) -> AutomaticReview | None:
    """Return the rule that allows the whole group, or None to ask the researcher."""
    classes = {action.review_class for action in group.actions}
    if not classes or any(action.risk not in {"low", "medium"} for action in group.actions):
        return None
    if mode == "review-changes" and len(classes) == 1:
        rule = _PROJECT_RULES.get(next(iter(classes)))
        return None if rule is None else AutomaticReview(mode=mode, rule=rule)
    if mode == "review-outside-effects" and sandboxed and classes <= _CONTAINED_CLASSES:
        return AutomaticReview(mode=mode, rule=_CONTAINED_RULE)
    return None


__all__ = [
    "AUTOMATIC_REVIEW_ACTOR",
    "REVIEW_MODES",
    "AutomaticReview",
    "automatic_review",
    "default_review_mode",
    "effective_review_mode",
    "is_tightening",
]
