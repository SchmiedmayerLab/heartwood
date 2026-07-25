# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Core harness orchestration for Heartwood sessions."""

from __future__ import annotations

from heartwood.core_adapter._facade import (
    AgentBackend,
    BackendEvent,
    BackendEventKind,
    DeterministicAgentBackend,
    LocalWorkspaceAgentBackend,
    ProposedToolCall,
    ToolExecution,
)
from heartwood.core_adapter._service import CommandConflictError, SessionResult, SessionService
from heartwood.core_adapter._state import (
    FileSessionStore,
    SessionOwnershipError,
    SessionRecoveryError,
    SessionStorageCapabilityError,
    SessionStoreBoundaryError,
)

__all__ = [
    "AgentBackend",
    "BackendEvent",
    "BackendEventKind",
    "CommandConflictError",
    "DeterministicAgentBackend",
    "FileSessionStore",
    "LocalWorkspaceAgentBackend",
    "ProposedToolCall",
    "SessionOwnershipError",
    "SessionRecoveryError",
    "SessionResult",
    "SessionService",
    "SessionStorageCapabilityError",
    "SessionStoreBoundaryError",
    "ToolExecution",
    "__version__",
]

__version__ = "0.2.0"
