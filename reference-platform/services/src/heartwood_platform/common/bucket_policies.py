# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Set the policies of the project buckets: python -m heartwood_platform.common.bucket_policies.

The object store keeps one policy for each bucket, and writing a policy replaces the
previous one. So one writer sets each bucket's policy with the statements of every
account that reaches it: the data service's account reads the objects of every project
bucket, and each project's job account reads its project's objects and writes below
``results/``. The chart's hook Job ``heartwood-bucket-policies`` runs it with the root
account after the accounts exist.
"""

from __future__ import annotations

import json
import re
import ssl
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from heartwood_platform.common.signing import Credentials, presign

_EXPIRES = 300
# The form of an access key that a policy names as its principal.
_ACCOUNT = r"^[A-Za-z0-9-]{1,128}$"


class Grant(BaseModel):
    """Actions that one account may take on the objects of a bucket below a prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The account's access key, or a file that holds it, when the chart cannot name it.
    account: str | None = Field(default=None, pattern=_ACCOUNT)
    account_file: Path | None = None
    actions: tuple[Literal["s3:GetObject", "s3:PutObject"], ...] = Field(min_length=1)
    # Empty for every object of the bucket, or a prefix that ends with a slash.
    prefix: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9-]*/)*$")

    @model_validator(mode="after")
    def _one_account(self) -> Self:
        if (self.account is None) == (self.account_file is None):
            raise ValueError("a grant names its account or the file of its account")
        return self


class PolicySettings(BaseModel):
    """The object store, its root account, and the grants of each bucket."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str = Field(pattern=r"^https://[^/\s]+$")
    region: str
    ca_file: Path
    root_access_key_file: Path
    root_secret_key_file: Path
    buckets: dict[str, tuple[Grant, ...]] = Field(min_length=1)

    @model_validator(mode="after")
    def _bucket_names(self) -> Self:
        for bucket, grants in self.buckets.items():
            if not bucket.startswith("hw-project-") or not grants:
                raise ValueError(f"{bucket} is no project bucket with grants")
        return self


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _account(grant: Grant) -> str:
    if grant.account is not None:
        return grant.account
    assert grant.account_file is not None
    account = _read(grant.account_file)
    # An empty or malformed key file would name no account or another principal.
    if re.fullmatch(_ACCOUNT, account) is None:
        raise ValueError(f"{grant.account_file.name} holds no access key")
    return account


def bucket_policy(bucket: str, grants: tuple[Grant, ...]) -> dict[str, Any]:
    """The policy of one bucket with a statement for each grant, in their order."""
    statements = [
        {
            "Effect": "Allow",
            "Principal": {"AWS": [_account(grant)]},
            "Action": list(grant.actions),
            "Resource": [f"arn:aws:s3:::{bucket}/{grant.prefix}*"],
        }
        for grant in grants
    ]
    return {"Version": "2012-10-17", "Statement": statements}


def write_policies(
    settings: PolicySettings,
    client: httpx.Client,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Mapping[str, dict[str, Any]]:
    """Replace the policy of each bucket with all of its grants; the step can be repeated."""
    root = Credentials(_read(settings.root_access_key_file), _read(settings.root_secret_key_file))
    policies = {
        bucket: bucket_policy(bucket, grants) for bucket, grants in sorted(settings.buckets.items())
    }
    for bucket, policy in policies.items():
        url = presign(
            method="PUT",
            endpoint=settings.endpoint,
            path=f"/{bucket}",
            credentials=root,
            region=settings.region,
            expires=_EXPIRES,
            now=clock(),
            query={"policy": ""},
        )
        response = client.put(url, content=json.dumps(policy).encode())
        if response.status_code not in {httpx.codes.OK, httpx.codes.NO_CONTENT}:
            raise RuntimeError(f"the policy of {bucket} was answered with {response.status_code}")
        print(f"set the policy of {bucket} with {len(policy['Statement'])} statements")
    return policies


def main() -> None:
    """Set the policies of the settings file named on the command line."""
    settings = PolicySettings.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    # The store serves HTTPS with a certificate of the platform CA, which is trusted alone.
    context = ssl.create_default_context(cafile=str(settings.ca_file))
    with httpx.Client(verify=context, timeout=30, follow_redirects=False) as client:
        write_policies(settings, client)


if __name__ == "__main__":
    main()
