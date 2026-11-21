# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Load the synthetic catalog into the object store: python -m heartwood_platform.data.seed.

The chart's bootstrap Job runs it with the object store's root account after the project
buckets exist. It writes each catalog object into its project's bucket; the data service's
account reads them through the bucket policies of heartwood_platform.common.bucket_policies.
"""

from __future__ import annotations

import hashlib
import ssl
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from heartwood_platform.common.signing import Credentials, presign

_EXPIRES = 300


class SeedObject(BaseModel):
    """A catalog object and the file that holds its bytes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str = Field(pattern=r"^hw-project-[a-z0-9-]+$")
    key: str
    file: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SeedSettings(BaseModel):
    """The object store, the accounts and the objects to load."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str = Field(pattern=r"^https://[^/]+$")
    region: str
    ca_file: Path
    root_access_key_file: Path
    root_secret_key_file: Path
    objects: tuple[SeedObject, ...]


def seed(
    settings: SeedSettings,
    client: httpx.Client,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Write the objects; the step can be repeated."""

    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8").strip()

    root = Credentials(read(settings.root_access_key_file), read(settings.root_secret_key_file))

    def put(path: str, content: bytes) -> None:
        url = presign(
            method="PUT",
            endpoint=settings.endpoint,
            path=path,
            credentials=root,
            region=settings.region,
            expires=_EXPIRES,
            now=clock(),
        )
        response = client.put(url, content=content)
        if response.status_code not in {httpx.codes.OK, httpx.codes.NO_CONTENT}:
            raise RuntimeError(f"PUT {path} answered {response.status_code}")

    for item in settings.objects:
        content = item.file.read_bytes()
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise RuntimeError(f"{item.file.name} differs from its catalog checksum")
        put(f"/{item.bucket}/{item.key}", content)
        print(f"wrote {item.bucket}/{item.key}")


def main() -> None:
    """Load the objects of the settings file named on the command line."""
    settings = SeedSettings.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    context = ssl.create_default_context(cafile=str(settings.ca_file))
    with httpx.Client(verify=context, timeout=30, follow_redirects=False) as client:
        seed(settings, client)


if __name__ == "__main__":
    main()
