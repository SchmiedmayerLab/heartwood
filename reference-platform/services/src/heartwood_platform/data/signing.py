# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Presigned S3 requests with AWS Signature Version 4.

A presigned URL authorizes exactly one method on one object path, with the query
parameters it names, until it expires. Whoever holds it needs no credential of their own.
The object store is addressed in path style: ``<endpoint>/<bucket>/<key>``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

# The longest validity Signature Version 4 allows.
MAX_EXPIRES_SECONDS = 7 * 24 * 3600
_ALGORITHM = "AWS4-HMAC-SHA256"
_SERVICE = "s3"


@dataclass(frozen=True)
class Credentials:
    """An access key of the object store."""

    access_key: str
    secret_key: str


def _encode(value: str, *, safe: str = "") -> str:
    # The unreserved characters of RFC 3986, as Signature Version 4 requires.
    return quote(value, safe="-_.~" + safe)


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def presign(
    *,
    method: str,
    endpoint: str,
    path: str,
    credentials: Credentials,
    region: str,
    expires: int,
    now: datetime,
    query: dict[str, str] | None = None,
) -> str:
    """Return a URL that performs ``method`` on ``path`` at ``endpoint`` until it expires.

    ``endpoint`` is the scheme and authority, such as ``https://store:7070``; ``path`` is the
    unencoded object path, such as ``/bucket/key``. ``now`` must be timezone-aware.
    """
    if not 1 <= expires <= MAX_EXPIRES_SECONDS:
        raise ValueError("a presigned URL expires after 1 second to 7 days")
    if now.tzinfo is None:
        raise ValueError("the signing time must be timezone-aware")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("the endpoint must be a scheme and an authority")
    if not path.startswith("/"):
        raise ValueError("the path must be absolute")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    scope = f"{day}/{region}/{_SERVICE}/aws4_request"
    parameters = {
        **(query or {}),
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": f"{credentials.access_key}/{scope}",
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{_encode(name)}={_encode(value)}" for name, value in sorted(parameters.items())
    )
    canonical_path = _encode(path, safe="/")
    canonical_request = "\n".join(
        [
            method,
            canonical_path,
            canonical_query,
            f"host:{parsed.netloc}\n",
            "host",
            "UNSIGNED-PAYLOAD",
        ]
    )
    string_to_sign = "\n".join(
        [_ALGORITHM, stamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    key = _hmac(f"AWS4{credentials.secret_key}".encode(), day)
    for part in (region, _SERVICE, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (
        f"{parsed.scheme}://{parsed.netloc}{canonical_path}"
        f"?{canonical_query}&X-Amz-Signature={signature}"
    )
