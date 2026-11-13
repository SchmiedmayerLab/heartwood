# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Admission and draining of calls owned by one gateway instance."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from threading import Condition, get_ident
from typing import Concatenate, Protocol, cast


class GatewayClosedError(RuntimeError):
    """Raised when a caller enters a gateway after shutdown admission closes."""


class GatewayOperations:
    """Let accepted calls finish without admitting new calls during shutdown."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._active: dict[int, int] = {}
        self._closing = False

    @contextmanager
    def enter(self) -> Iterator[None]:
        thread_id = get_ident()
        with self._condition:
            depth = self._active.get(thread_id, 0)
            if self._closing and depth == 0:
                raise GatewayClosedError("gateway is shutting down or stopped")
            self._active[thread_id] = depth + 1
        try:
            yield
        finally:
            with self._condition:
                depth = self._active[thread_id] - 1
                if depth:
                    self._active[thread_id] = depth
                else:
                    del self._active[thread_id]
                    self._condition.notify_all()

    def close_admission(self) -> None:
        with self._condition:
            if get_ident() in self._active:
                raise RuntimeError("cannot stop the gateway from inside an active gateway call")
            self._closing = True

    def wait_for_idle(self, *, timeout: float) -> None:
        with self._condition:
            if not self._condition.wait_for(lambda: not self._active, timeout=timeout):
                raise TimeoutError("gateway calls are still active; shutdown is incomplete")


class _OperationOwner(Protocol):
    _operations: GatewayOperations


def gateway_operation[Owner: _OperationOwner, **Parameters, Return](
    method: Callable[Concatenate[Owner, Parameters], Return],
) -> Callable[Concatenate[Owner, Parameters], Return]:
    @wraps(method)
    def admitted(self: Owner, *args: Parameters.args, **kwargs: Parameters.kwargs) -> Return:
        with self._operations.enter():
            return method(self, *args, **kwargs)

    return cast(Callable[Concatenate[Owner, Parameters], Return], admitted)
