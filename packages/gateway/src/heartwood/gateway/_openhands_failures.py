# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Privacy-safe translation of OpenHands runtime failures."""

from __future__ import annotations

import logging

from openhands.sdk.event.error_classification import (
    ErrorClassification,
    FailureKind,
    classify_error,
)

from heartwood.core_adapter import (
    BackendErrorCode,
    BackendErrorEvent,
    BackendEvent,
)


class _PrivacySafeRetryLogFilter(logging.Filter):
    """Replace provider exception text with OpenHands' safe failure category."""

    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args or ()
        error = next(
            (argument for argument in arguments if isinstance(argument, BaseException)),
            None,
        )
        if error is None:
            return True
        classification = classify_error(type(error).__name__, str(error))
        attempt = getattr(error, "retry_attempt", None)
        record.msg = (
            "Model provider request failed (%s)."
            if not isinstance(attempt, int)
            else "Model provider request failed (%s); retry attempt %d."
        )
        record.args = (
            (classification.kind.value,)
            if not isinstance(attempt, int)
            else (classification.kind.value, attempt)
        )
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def install_privacy_safe_retry_logging() -> None:
    """Scrub raw provider exceptions from OpenHands retry log records."""
    logger = logging.getLogger("openhands.sdk.llm.utils.retry_mixin")
    if not any(isinstance(item, _PrivacySafeRetryLogFilter) for item in logger.filters):
        logger.addFilter(_PrivacySafeRetryLogFilter())


def backend_error(
    error: Exception,
    *,
    source_event_id: str | None = None,
) -> BackendEvent:
    """Translate a locally caught exception without retaining its text."""
    return BackendErrorEvent(
        error_code=classified_error_code(
            classify_error(type(error).__name__, str(error)),
            fallback=BackendErrorCode.WORKER_STOPPED,
        ),
        source_event_id=source_event_id,
    )


def classified_error_code(
    classification: ErrorClassification | None,
    *,
    fallback: BackendErrorCode,
) -> BackendErrorCode:
    """Map OpenHands' privacy-safe failure vocabulary to stable diagnostics."""
    if classification is None:
        return fallback
    if classification.kind == FailureKind.AUTH:
        return BackendErrorCode.PROVIDER_AUTHENTICATION_FAILED
    if classification.kind == FailureKind.QUOTA:
        return BackendErrorCode.PROVIDER_QUOTA_EXHAUSTED
    if classification.kind == FailureKind.RATE_LIMIT:
        return BackendErrorCode.PROVIDER_RATE_LIMITED
    if classification.kind == FailureKind.CONFIG:
        return BackendErrorCode.MODEL_CONFIGURATION_INVALID
    if classification.kind == FailureKind.TRANSIENT:
        return BackendErrorCode.PROVIDER_UNAVAILABLE
    if classification.kind == FailureKind.AGENT_ACTION:
        return (
            BackendErrorCode.ACTION_FAILED
            if classification.retryable
            else BackendErrorCode.CONVERSATION_STOPPED
        )
    if classification.kind == FailureKind.INTERNAL:
        return BackendErrorCode.WORKER_STOPPED
    return fallback


__all__ = [
    "backend_error",
    "classified_error_code",
    "install_privacy_safe_retry_logging",
]
