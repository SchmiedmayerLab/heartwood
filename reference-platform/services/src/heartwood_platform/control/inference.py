# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The managed model route of sessions: virtual keys in LiteLLM and their settlement.

Each session has one virtual key for its workers and one for model discovery by its
Heartwood gateway. The worker key lists the route's models and only the inference routes,
and its budget is the session's reservation minus the cost of one request, because the
proxy checks spending before a request and the last admitted request can end above the
budget. The key lives only in the provider of the session's OpenShell gateway. The
discovery key may only list models. The control service keeps the hashes of both keys.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path

from heartwood_platform.control.litellm import KeyInfo, LiteLLM, key_hash
from heartwood_platform.control.openshell import OpenShellProviders
from heartwood_platform.control.prices import PriceSheet

# The routes of a worker key: requests and the model list.
WORKER_ROUTES = ("/v1/chat/completions", "/v1/models")
DISCOVERY_ROUTES = ("/v1/models",)
# The discovery key cannot call a model; its budget is the smallest amount nonetheless.
_DISCOVERY_BUDGET = Decimal("0.000001")
_PRECISION = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_key (
    session TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    discovery TEXT,
    max_budget TEXT NOT NULL,
    carried TEXT NOT NULL,
    settled TEXT,
    created_at TEXT NOT NULL
) STRICT
"""


class InferenceSetupError(Exception):
    """The route's models or prices do not allow a budgeted session key."""


@dataclass(frozen=True)
class RouteModel:
    """A model of the route and the largest request it accepts."""

    name: str
    max_input_tokens: int
    max_output_tokens: int
    supports_tool_calls: bool = False


def request_headroom(models: tuple[RouteModel, ...], prices: PriceSheet) -> Decimal:
    """Return the largest cost of one request to any model of the route.

    Each model's runtime profile in the price sheet has the model's name and prices its
    input and output tokens.
    """
    if not models:
        raise InferenceSetupError("the model route has no model")
    costs = []
    for model in models:
        profile = prices.profile(model.name)
        if profile is None or profile.kind != "inference":
            raise InferenceSetupError(f"the price sheet has no inference price for {model.name}")
        tokens = {"input_tokens": model.max_input_tokens, "output_tokens": model.max_output_tokens}
        with localcontext(prec=_PRECISION):
            costs.append(
                sum(
                    (
                        Decimal(tokens[rate.measure])
                        * Decimal(prices.price(rate.sku_price_id).unit_price)
                        / Decimal(rate.quantity_per_pricing_unit)
                        for rate in profile.rates
                    ),
                    Decimal(0),
                )
            )
    return max(costs)


@dataclass(frozen=True)
class SessionRoute:
    """Where the session's OpenShell gateway is and how to authenticate to it."""

    endpoint: str
    client: dict[str, str]


@dataclass(frozen=True)
class _Keys:
    token: str
    discovery: str | None
    max_budget: Decimal
    carried: Decimal
    settled: Decimal | None


class ManagedInference:
    """Creates, rotates and settles the virtual keys of sessions."""

    def __init__(
        self,
        database: Path,
        proxy: LiteLLM,
        providers: OpenShellProviders,
        models: tuple[RouteModel, ...],
        prices: PriceSheet,
        *,
        settle_wait_seconds: float,
        poll_seconds: float = 5.0,
        polls: int = 12,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Keep the key hashes in ``database`` and call the proxy and the OpenShell gateways.

        A settlement or rotation blocks the key, waits ``settle_wait_seconds`` for the
        proxy to record the spending of the last requests, and then reads the spending
        every ``poll_seconds`` until it stops changing.
        """
        self._database = database
        self._proxy = proxy
        self._providers = providers
        self._models = tuple(model.name for model in models)
        self.headroom = request_headroom(models, prices)
        self._settle_wait = settle_wait_seconds
        self._poll = poll_seconds
        self._polls = polls
        self._sleep = sleep
        self._clock = clock
        with self._transaction() as connection:
            connection.execute(_SCHEMA)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=30, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _keys(self, session: str) -> _Keys | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT token, discovery, max_budget, carried, settled FROM session_key"
                " WHERE session = ?",
                (session,),
            ).fetchone()
        if row is None:
            return None
        return _Keys(
            row[0],
            row[1],
            Decimal(row[2]),
            Decimal(row[3]),
            None if row[4] is None else Decimal(row[4]),
        )

    def _store(self, session: str, keys: _Keys) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO session_key VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (session) DO UPDATE SET token = excluded.token,"
                " discovery = excluded.discovery, max_budget = excluded.max_budget,"
                " carried = excluded.carried, settled = excluded.settled",
                (
                    session,
                    keys.token,
                    keys.discovery,
                    str(keys.max_budget),
                    str(keys.carried),
                    None if keys.settled is None else str(keys.settled),
                    self._clock().isoformat(),
                ),
            )

    def _generate(self, session: str, project: str, max_budget: Decimal) -> str:
        return self._proxy.generate_key(
            alias=f"hw-session-{session}-{secrets.token_hex(4)}",
            models=self._models,
            allowed_routes=WORKER_ROUTES,
            max_budget=max_budget,
            metadata={"session": session, "project": project, "purpose": "worker"},
        )

    def prepare(
        self, session: str, project: str, reservation: Decimal, route: SessionRoute
    ) -> dict[str, str]:
        """Give a session its worker key and a new discovery key.

        The worker key is created at the first launch and kept afterwards, with the
        route's current models, so a model removed from the route is refused at once. A
        session whose OpenShell gateway lost the provider gets a rotated key. The reply
        holds the files for the gateway's Secret.
        """
        keys = self._keys(session)
        if keys is not None and keys.settled is not None:
            raise InferenceSetupError("the session's inference is settled")
        if keys is None:
            # The budget leaves room for the request that ends above it.
            with localcontext(prec=_PRECISION):
                budget = reservation - self.headroom
            key = self._generate(session, project, budget)
            keys = _Keys(key_hash(key), None, budget, Decimal(0), None)
            self._store(session, keys)
            self._providers.apply(route.endpoint, route.client, key, replace=True)
        else:
            self._proxy.update_key(keys.token, models=self._models)
            if not self._providers.exists(route.endpoint, route.client):
                keys = self._rotate(session, project, keys, route)
        discovery = self._proxy.generate_key(
            alias=f"hw-discovery-{session}-{secrets.token_hex(4)}",
            models=self._models,
            allowed_routes=DISCOVERY_ROUTES,
            max_budget=_DISCOVERY_BUDGET,
            metadata={"session": session, "project": project, "purpose": "discovery"},
        )
        previous = keys.discovery
        self._store(session, _Keys(keys.token, key_hash(discovery), *_rest(keys)))
        if previous is not None:
            self._proxy.delete_key(previous)
        return {"model-discovery-key": discovery}

    def _measure(self, token: str) -> Decimal:
        """Block a key, wait for its last requests to be recorded and return its spending."""
        info = self._proxy.key_info(token)
        if info is None:
            return Decimal(0)
        # The proxy refuses a request when the spending reaches the budget, and it counts
        # spending in memory before it records it.
        self._proxy.update_key(token, max_budget=info.spend)
        self._sleep(self._settle_wait)
        last: KeyInfo | None = None
        for _ in range(self._polls):
            current = self._proxy.key_info(token)
            if current is None:
                break
            if last is not None and current.spend == last.spend:
                return current.spend
            last = current
            self._sleep(self._poll)
        if last is None:
            return info.spend
        return last.spend

    def _rotate(self, session: str, project: str, keys: _Keys, route: SessionRoute) -> _Keys:
        spend = self._measure(keys.token)
        with localcontext(prec=_PRECISION):
            carried = keys.carried + spend
            budget = max(keys.max_budget - carried, Decimal(0))
        key = self._generate(session, project, budget)
        rotated = _Keys(key_hash(key), keys.discovery, keys.max_budget, carried, None)
        self._store(session, rotated)
        self._providers.apply(route.endpoint, route.client, key, replace=True)
        self._proxy.delete_key(keys.token)
        return rotated

    def rotate(self, session: str, project: str, route: SessionRoute) -> None:
        """Replace the worker key of a session; the new key keeps the remaining budget.

        Workers attached to the previous key fail; the next worker uses the new key.
        """
        keys = self._keys(session)
        if keys is None or keys.settled is not None:
            raise InferenceSetupError("the session has no key to rotate")
        self._rotate(session, project, keys, route)

    def settle(self, session: str) -> Decimal:
        """Stop a session's inference and return what all of its keys incurred.

        The amount is recorded before the keys are deleted, so a repeated call returns it.
        """
        keys = self._keys(session)
        if keys is None:
            return Decimal(0)
        if keys.settled is None:
            spend = self._measure(keys.token)
            with localcontext(prec=_PRECISION):
                settled = keys.carried + spend
            keys = _Keys(keys.token, keys.discovery, keys.max_budget, keys.carried, settled)
            self._store(session, keys)
        self._proxy.delete_key(keys.token)
        if keys.discovery is not None:
            self._proxy.delete_key(keys.discovery)
        assert keys.settled is not None
        return keys.settled

    def forget(self, session: str) -> None:
        """Remove the record of a deleted session's keys."""
        with self._transaction() as connection:
            connection.execute("DELETE FROM session_key WHERE session = ?", (session,))


def _rest(keys: _Keys) -> tuple[Decimal, Decimal, Decimal | None]:
    return keys.max_budget, keys.carried, keys.settled
