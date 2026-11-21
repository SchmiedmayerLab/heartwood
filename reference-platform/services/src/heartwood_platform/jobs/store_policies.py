# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Set the bucket policies of the job accounts: python -m heartwood_platform.jobs.store_policies.

The chart's store bootstrap Job runs it with the object store's root account after the
project buckets and the job accounts exist. Each project's job account may read the
objects of the project's bucket and write below its results prefix, and nothing else.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from heartwood_platform.common.signing import Credentials, presign

_EXPIRES = 300


def set_policies(
    policies: dict[str, object],
    endpoint: str,
    root: Credentials,
    client: httpx.Client,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Replace the policy of each bucket; the step can be repeated."""
    for bucket, policy in sorted(policies.items()):
        url = presign(
            method="PUT",
            endpoint=endpoint,
            path=f"/{bucket}",
            credentials=root,
            region="us-east-1",
            expires=_EXPIRES,
            now=clock(),
            query={"policy": ""},
        )
        response = client.put(url, content=json.dumps(policy).encode())
        if response.status_code not in {httpx.codes.OK, httpx.codes.NO_CONTENT}:
            raise RuntimeError(f"the policy of {bucket} was answered with {response.status_code}")
        print(f"set the job policy of {bucket}")


def main() -> None:
    """Set the policies of the file named on the command line."""
    policies = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    root = Credentials(os.environ["ADMIN_ACCESS_KEY"], os.environ["ADMIN_SECRET_KEY"])
    # The store serves HTTPS with a certificate of the platform CA, which the client trusts alone.
    context = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    with httpx.Client(verify=context, timeout=30, follow_redirects=False) as client:
        set_policies(policies, os.environ["STORE_ENDPOINT"], root, client)


if __name__ == "__main__":
    main()
