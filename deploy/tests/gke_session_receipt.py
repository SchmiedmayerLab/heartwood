# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Retain the exit status of the attended GKE pytest session in its run directory."""

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(exitstatus: int) -> Iterator[None]:
    """Make completion visible after pytest has finalized its test report."""
    yield
    directory = Path(os.environ["HW_QUALIFICATION_OUTPUT"])
    expected = Path("/qualification/output") / (os.environ["GITHUB_RUN_ID"] + ".1")
    if directory != expected or os.environ["GITHUB_RUN_ATTEMPT"] != "1":
        raise ValueError("unexpected qualification output directory or run attempt")
    paths = [directory / "junit.xml", *sorted(directory.glob("test_*/evidence.json"))]
    hashes = {}
    for path in paths:
        if path.resolve() != path or not path.is_file():
            raise ValueError("qualification output is not a regular file")
        hashes[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = directory / "session-receipt.partial"
    with receipt.open("x") as stream:
        json.dump(
            {
                "run": int(os.environ["GITHUB_RUN_ID"]),
                "attempt": 1,
                "commit": os.environ["GITHUB_SHA"],
                "exitstatus": int(exitstatus),
                "files": hashes,
            },
            stream,
        )
    receipt.rename(directory / "session-receipt.json")
