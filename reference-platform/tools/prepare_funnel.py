# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Prepare the build context of the Funnel image from the pinned source and its patches.

The source is the revision archive that ``images/funnel/funnel.json`` names.
GitHub generates revision archives on request and does not keep their bytes stable.
The manifest records ``tree_sha256``, the digest of the extracted tree, instead.
Each patch is verified by its SHA-256 digest and applied in order with git apply.
The patched tree must match ``patched_tree_sha256``.
It is written to ``OUTPUT/funnel``; ``OUTPUT`` is the build context of the Dockerfile.
With ``--image TAG`` the image is built from that context.
With ``--test`` the tests of the patched packages run first.

Usage: prepare_funnel.py OUTPUT [--cache DIRECTORY] [--image TAG [--test]]
       prepare_funnel.py --print-tree-digest
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any

FUNNEL = Path(__file__).resolve().parents[1] / "images" / "funnel"
MANIFEST = FUNNEL / "funnel.json"
# The archive of the pinned revision is about 1.3 MiB.
MAX_ARCHIVE = 64 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")


class SourceError(Exception):
    """The source or a patch does not match the manifest."""


def _load_candidate_builder() -> ModuleType:
    """Load the source verification of ``deploy/build-worker-candidate.py``.

    Funnel's source is verified by the same extraction and tree digest as the worker's
    sources, so both follow one implementation.
    """
    path = Path(__file__).resolve().parents[2] / "deploy" / "build-worker-candidate.py"
    specification = importlib.util.spec_from_file_location("build_worker_candidate", path)
    if specification is None or specification.loader is None:
        raise SourceError("the source verification cannot be loaded")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_CANDIDATE = _load_candidate_builder()


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def tree_digest(root: Path) -> str:
    """Identify a tree by its paths, kinds, executable bits, contents and link targets."""
    try:
        digest = _CANDIDATE._tree_digest(root)
    except ValueError as error:
        raise SourceError(str(error)) from error
    assert isinstance(digest, str)
    return digest


def _expected(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise SourceError(f"{key} is not a SHA-256 digest")
    return value


def extract(source: dict[str, Any], staging: Path, cache: Path | None = None) -> Path:
    """Extract the source into ``staging`` and return its verified root directory.

    A cached archive is verified like a downloaded one, and only an archive whose tree
    matched is cached.
    """
    verified = {"url": source["url"], "tree_sha256": _expected(source, "tree_sha256")}
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
    try:
        checkout = _CANDIDATE._source(verified, staging, cache=cache, limit=MAX_ARCHIVE)
    except ValueError as error:
        raise SourceError(str(error)) from error
    assert isinstance(checkout, Path)
    return checkout


def _git_environment(checkout: Path) -> dict[str, str]:
    """Isolate git from the caller's repository, configuration and environment."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return {
        **environment,
        # The checkout is not a repository, and git must not find an enclosing one.
        "GIT_CEILING_DIRECTORIES": str(checkout.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def apply_patches(checkout: Path, patches: list[dict[str, str]], directory: Path) -> None:
    """Apply each patch after verifying its digest; a patch that does not apply exactly fails."""
    for patch in patches:
        content = (directory / patch["path"]).read_bytes()
        if _digest(content) != patch["sha256"]:
            raise SourceError(f"{patch['path']} does not match its recorded digest")
        # git apply applies all of a patch or nothing, and whitespace is matched and kept exactly.
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=checkout,
            input=content,
            capture_output=True,
            env=_git_environment(checkout),
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.decode(errors="replace").strip()
            raise SourceError(f"{patch['path']} does not apply: {message}")


def prepare(output: Path, cache: Path | None = None, manifest: Path = MANIFEST) -> Path:
    """Write the patched source to ``output/funnel`` and return that directory.

    ``output`` must be absent or empty, because it becomes the image's build context.
    """
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    expected = _expected(recorded["source"], "patched_tree_sha256")
    if output.exists() and any(output.iterdir()):
        raise SourceError(f"{output} is not empty")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="funnel-source-", dir=output.parent) as temporary:
        checkout = extract(recorded["source"], Path(temporary) / "source", cache)
        apply_patches(checkout, recorded["patches"], manifest.parent)
        actual = tree_digest(checkout)
        if actual != expected:
            raise SourceError(f"the patched tree digest is {actual}")
        target = output / "funnel"
        shutil.move(checkout, target)
    return target


def build(context: Path, tag: str, *, test: bool, manifest: Path = MANIFEST) -> None:
    """Build the image from a prepared context, running the patch tests first on request."""
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    command = [
        "docker",
        "build",
        "--file",
        str(manifest.parent / "Dockerfile"),
        "--build-arg",
        f"FUNNEL_VERSION={recorded['version']}",
        "--build-arg",
        f"FUNNEL_REVISION={recorded['source']['revision']}",
    ]
    if test:
        tests = " ".join(recorded["tests"])
        subprocess.run(
            [*command, "--target", "test", "--build-arg", f"FUNNEL_TESTS={tests}", str(context)],
            check=True,
        )
    subprocess.run([*command, "--tag", tag, str(context)], check=True)


def print_tree_digest(manifest: Path = MANIFEST) -> str:
    """Return the tree digest of the archive that the manifest names, without comparing it.

    The value is trusted only after it matches a reviewed checkout of the revision.
    """
    source = json.loads(manifest.read_text(encoding="utf-8"))["source"]
    with TemporaryDirectory() as temporary:
        try:
            extract({**source, "tree_sha256": "0" * 64}, Path(temporary) / "source")
        except SourceError as error:
            message = str(error)
            if "tree_sha256 is " not in message:
                raise
            return message.rsplit(" ", 1)[1]
    raise SourceError("the source unexpectedly matched an empty digest")


def main() -> int:
    """Prepare the build context named on the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--image", help="build the image under this tag")
    parser.add_argument("--test", action="store_true", help="run the patch tests before")
    parser.add_argument(
        "--print-tree-digest",
        action="store_true",
        help="print the tree digest of the downloaded source instead of verifying it",
    )
    arguments = parser.parse_args()
    try:
        if arguments.print_tree_digest:
            print(print_tree_digest())
            return 0
        if arguments.output is None:
            parser.error("OUTPUT is required")
        if arguments.test and not arguments.image:
            parser.error("--test requires --image")
        prepare(arguments.output, arguments.cache)
    except SourceError as error:
        print(f"prepare_funnel: {error}", file=sys.stderr)
        return 1
    if arguments.image:
        build(arguments.output, arguments.image, test=arguments.test)
    return 0


if __name__ == "__main__":
    sys.exit(main())
