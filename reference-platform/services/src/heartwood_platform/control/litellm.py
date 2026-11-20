# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The key operations of the LiteLLM proxy that the control service uses.

The control service is the only holder of the master key. It creates the virtual keys of
sessions, changes their budgets and model lists, reads their spending and deletes them.
After creation it refers to a key only by its SHA-256 hash, which the proxy accepts in
place of the key, so no key is stored.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx


class InferenceGatewayError(Exception):
    """The proxy answered a key operation unexpectedly."""


def key_hash(key: str) -> str:
    """Return the hash under which the proxy stores a key."""
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class KeyInfo:
    """The spending and budget of a key, as the proxy last recorded them."""

    spend: Decimal
    max_budget: Decimal | None


def _amount(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InferenceGatewayError("the proxy reported an amount that is not a number")
    if value < 0:
        raise InferenceGatewayError("the proxy reported a negative amount")
    # The proxy keeps amounts as floating-point numbers; their shortest decimal form is
    # the amount it reports.
    return Decimal(repr(float(value)))


def _budget(value: Decimal) -> float:
    return float(value)


class LiteLLM:
    """Key management calls of the proxy, authenticated with the master key."""

    def __init__(self, client: httpx.Client, master_key: Callable[[], str]) -> None:
        """Call the proxy through ``client``, which trusts the platform CA."""
        self._client = client
        self._master_key = master_key

    def _call(self, method: str, path: str, **arguments: Any) -> dict[str, Any]:  # noqa: ANN401
        response = self._client.request(
            method, path, headers={"Authorization": f"Bearer {self._master_key()}"}, **arguments
        )
        if response.status_code != httpx.codes.OK:
            # The answer may echo a request field; only the status is reported.
            raise InferenceGatewayError(f"the proxy answered {path} with {response.status_code}")
        document = response.json()
        if not isinstance(document, dict):
            raise InferenceGatewayError(f"the proxy answered {path} without an object")
        return document

    def generate_key(
        self,
        *,
        alias: str,
        models: Sequence[str],
        allowed_routes: Sequence[str],
        max_budget: Decimal,
        metadata: dict[str, str],
        max_parallel_requests: int | None = None,
    ) -> str:
        """Create a key for listed models and routes, with a budget, and return it.

        A key with an empty model list may call every model of the proxy, so one is
        refused here.
        """
        if not models or not allowed_routes:
            raise ValueError("a key names its models and routes")
        document = self._call(
            "POST",
            "/key/generate",
            json={
                "key_alias": alias,
                "models": list(models),
                "allowed_routes": list(allowed_routes),
                "max_budget": _budget(max_budget),
                "metadata": metadata,
                **(
                    {}
                    if max_parallel_requests is None
                    else {"max_parallel_requests": max_parallel_requests}
                ),
            },
        )
        key = document.get("key")
        if not isinstance(key, str) or not key.startswith("sk-"):
            raise InferenceGatewayError("the proxy returned no key")
        return key

    def update_key(
        self,
        token: str,
        *,
        max_budget: Decimal | None = None,
        models: Sequence[str] | None = None,
    ) -> None:
        """Change the budget or the model list of the key with a hash."""
        fields: dict[str, Any] = {"key": token}
        if max_budget is not None:
            fields["max_budget"] = _budget(max_budget)
        if models is not None:
            if not models:
                raise ValueError("a key names its models")
            fields["models"] = list(models)
        self._call("POST", "/key/update", json=fields)

    def key_info(self, token: str) -> KeyInfo | None:
        """Return the spending and budget of the key with a hash, or None if it is gone."""
        response = self._client.get(
            "/key/info",
            params={"key": token},
            headers={"Authorization": f"Bearer {self._master_key()}"},
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        if response.status_code != httpx.codes.OK:
            raise InferenceGatewayError(f"the proxy answered /key/info with {response.status_code}")
        document = response.json()
        info = document.get("info") if isinstance(document, dict) else None
        if not isinstance(info, dict):
            raise InferenceGatewayError("the proxy answered /key/info without the key")
        budget = info.get("max_budget")
        return KeyInfo(
            spend=_amount(info.get("spend", 0)),
            max_budget=None if budget is None else _amount(budget),
        )

    def delete_key(self, token: str) -> None:
        """Delete the key with a hash, whether or not it exists."""
        response = self._client.post(
            "/key/delete",
            json={"keys": [token]},
            headers={"Authorization": f"Bearer {self._master_key()}"},
        )
        if response.status_code not in {httpx.codes.OK, httpx.codes.NOT_FOUND}:
            raise InferenceGatewayError(
                f"the proxy answered /key/delete with {response.status_code}"
            )
