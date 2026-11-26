# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Researcher identifiers, which name a researcher in platform paths and URLs.

The control service's paths, the Hub's user names, the gateways' base paths and the
redirect URIs name a researcher by an identifier derived from the issuer and the
subject under the platform's researcher identifier key, so no subject appears in a
URL or in the logs of the services that see them. The Hub derives the same identifier
when a researcher signs in (files/hub/heartwood_hub.py).
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, StringConstraints

# ``r`` and 128 bits of the digest in lowercase hexadecimal, a valid Hub user name.
RESEARCHER_ID_PATTERN = r"^r[0-9a-f]{32}$"
# The gateways require at least this much key material for the researcher actor
# (MINIMUM_ACTOR_KEY_BYTES in heartwood.gateway._identity); the same holds here.
MINIMUM_KEY_BYTES = 32
# The gateways read at most this many bytes of a key file, trailing newline included.
MAXIMUM_KEY_FILE_BYTES = 4096


def _plain_subject(value: str) -> str:
    if value.strip() != value or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
    ):
        raise ValueError("a subject has no control or format characters or surrounding spaces")
    return value


# Any OpenID Connect subject, as the gateways accept it.
type Subject = Annotated[
    str, StringConstraints(min_length=1, max_length=255), AfterValidator(_plain_subject)
]


def researcher_id(issuer: str, subject: str, key: bytes) -> str:
    """Return the identifier of the researcher with ``subject`` at ``issuer``."""
    digest = hmac.new(key, f"{issuer}\0{subject}".encode(), hashlib.sha256).hexdigest()
    return f"r{digest[:32]}"


def read_key(path: Path) -> str:
    """Read a platform key file; a missing, oversized, non-UTF-8 or short key stops the service."""
    content = path.read_bytes()
    if len(content) > MAXIMUM_KEY_FILE_BYTES:
        raise ValueError(f"the key file {path.name} exceeds {MAXIMUM_KEY_FILE_BYTES} bytes")
    try:
        key = content.decode("utf-8").removesuffix("\n")
    except UnicodeDecodeError:
        raise ValueError(f"the key file {path.name} is not UTF-8") from None
    if len(key.encode()) < MINIMUM_KEY_BYTES:
        raise ValueError(f"the key in {path.name} needs at least {MINIMUM_KEY_BYTES} bytes")
    return key
