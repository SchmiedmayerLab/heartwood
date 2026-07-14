# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Shared validation for immutable Hugging Face model identities."""

from __future__ import annotations

import re

from huggingface_hub.errors import HFValidationError
from huggingface_hub.utils import validate_repo_id  # type: ignore[attr-defined]

_IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{7,64}$")
_RESOLVED_REVISION = re.compile(r"^[0-9a-f]{40,64}$")


def is_hugging_face_model_id(value: str) -> bool:
    """Return whether ``value`` is an explicit Hugging Face owner/model id."""
    if value.count("/") != 1:
        return False
    try:
        validate_repo_id(value)
    except HFValidationError:
        return False
    return True


def is_immutable_revision(value: str) -> bool:
    """Return whether ``value`` can identify an immutable hexadecimal revision."""
    return _IMMUTABLE_REVISION.fullmatch(value) is not None


def is_resolved_revision(value: str) -> bool:
    """Return whether ``value`` is a full hexadecimal commit revision."""
    return _RESOLVED_REVISION.fullmatch(value) is not None
