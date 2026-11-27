# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Short-lived access to the objects of the catalog.

The data service holds a read-only account of the object store and signs one URL for
each object. A URL allows reading that object only, and its validity grows with the
object's size, so a large object can be read completely and a small one stays valid
briefly.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from heartwood_platform.common.signing import Credentials, presign
from heartwood_platform.data.catalog import Entry

# The validity of a URL: a fixed allowance for starting the copy pod, which may include
# provisioning its volume and pulling its image, plus one second for each MiB at the
# slowest transfer rate the platform supports, up to one hour.
BASE_SECONDS = 300
SECONDS_PER_MIB = 1
MAX_SECONDS = 3600
_MIB = 1024 * 1024


def access_seconds(size: int) -> int:
    """Return how long a URL for an object of ``size`` bytes stays valid."""
    return min(MAX_SECONDS, BASE_SECONDS + math.ceil(size / _MIB) * SECONDS_PER_MIB)


@dataclass(frozen=True)
class AccessUrl:
    """A presigned URL for one object and the time it expires."""

    url: str
    expires_at: datetime


class ObjectAccess:
    """Signs read URLs for catalog objects with the data service's account."""

    def __init__(
        self,
        endpoint: str,
        region: str,
        credentials: Callable[[], Credentials],
        clock: Callable[[], datetime],
    ) -> None:
        """Sign for the object store at ``endpoint``; credentials are read for each URL."""
        self._endpoint = endpoint
        self._region = region
        self._credentials = credentials
        self._clock = clock

    def url(self, entry: Entry) -> AccessUrl:
        """Return a URL that reads the object of ``entry`` until it expires."""
        now = self._clock()
        seconds = access_seconds(entry.item.size)
        url = presign(
            method="GET",
            endpoint=self._endpoint,
            path=f"/{entry.bucket}/{entry.key}",
            credentials=self._credentials(),
            region=self._region,
            expires=seconds,
            now=now,
        )
        return AccessUrl(url=url, expires_at=now + timedelta(seconds=seconds))
