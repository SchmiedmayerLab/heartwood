# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Core harness orchestration for Heartwood sessions."""

from __future__ import annotations

from heartwood.core_adapter._facade import (
    AgentBackend,
    BackendAgentMessageEvent,
    BackendConfirmationRequestEvent,
    BackendConfirmationResolutionEvent,
    BackendErrorCode,
    BackendErrorEvent,
    BackendEvent,
    BackendEventKind,
    BackendEventSink,
    BackendLifecycle,
    BackendLifecycleEvent,
    BackendSubagent,
    BackendSubagentEvent,
    BackendSubagentStatus,
    BackendTask,
    BackendTaskPlanEvent,
    BackendTaskStatus,
    BackendToolCallEvent,
    BackendToolExecutionEvent,
    BackendUsage,
    BackendUsageEvent,
    DeterministicAgentBackend,
    LocalWorkspaceAgentBackend,
    PendingActionGroup,
    ProposedToolCall,
    TokenDeltaSink,
    ToolExecution,
    backend_error_is_fatal,
    backend_error_message,
    pending_action_group,
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
    "BackendAgentMessageEvent",
    "BackendConfirmationRequestEvent",
    "BackendConfirmationResolutionEvent",
    "BackendErrorCode",
    "BackendErrorEvent",
    "BackendEvent",
    "BackendEventKind",
    "BackendEventSink",
    "BackendLifecycle",
    "BackendLifecycleEvent",
    "BackendSubagent",
    "BackendSubagentEvent",
    "BackendSubagentStatus",
    "BackendTask",
    "BackendTaskPlanEvent",
    "BackendTaskStatus",
    "BackendToolCallEvent",
    "BackendToolExecutionEvent",
    "BackendUsage",
    "BackendUsageEvent",
    "CommandConflictError",
    "DeterministicAgentBackend",
    "FileSessionStore",
    "LocalWorkspaceAgentBackend",
    "PendingActionGroup",
    "ProposedToolCall",
    "SessionOwnershipError",
    "SessionRecoveryError",
    "SessionResult",
    "SessionService",
    "SessionStorageCapabilityError",
    "SessionStoreBoundaryError",
    "TokenDeltaSink",
    "ToolExecution",
    "__version__",
    "backend_error_is_fatal",
    "backend_error_message",
    "pending_action_group",
]

__version__ = "0.3.0-beta.2"
