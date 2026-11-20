# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Prepare the build context of the Funnel image from the pinned source and its patches.

The source is the revision archive that ``images/funnel/funnel.json`` names.
GitHub generates revision archives on request and does not keep their bytes stable.
The manifest records ``tree_sha256``, the digest of the extracted tree, instead.
Each patch is verified by its SHA-256 digest and applied in order without fuzz.
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
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

FUNNEL = Path(__file__).resolve().parents[1] / "images" / "funnel"
MANIFEST = FUNNEL / "funnel.json"
# The archive of the pinned revision is about 20 MiB.
MAX_ARCHIVE = 128 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")


class SourceError(Exception):
    """The source or a patch does not match the manifest."""


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def tree_digest(root: Path) -> str:
    """Identify a tree by its paths, kinds, executable bits, contents and link targets.

    Archive metadata such as compression, timestamps and ownership is excluded.
    A regenerated archive of the same revision therefore keeps its digest.
    """
    entries: list[tuple[bytes, bytes, bytes]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                path = os.fsencode(Path(child.path).relative_to(root).as_posix())
                if child.is_symlink():
                    # The raw target, since Path.readlink normalizes it.
                    target = os.readlink(child.path)  # noqa: PTH115
                    entries.append((path, b"symlink", os.fsencode(target)))
                elif child.is_dir(follow_symlinks=False):
                    entries.append((path, b"directory", b""))
                    pending.append(Path(child.path))
                elif child.is_file(follow_symlinks=False):
                    executable = child.stat(follow_symlinks=False).st_mode & 0o100
                    kind = b"executable" if executable else b"file"
                    entries.append((path, kind, _digest(Path(child.path).read_bytes()).encode()))
                else:
                    raise SourceError("the source contains a special file")
    # NUL cannot occur in a path or link target, so each record parses unambiguously.
    return _digest(b"".join(b"\0".join(entry) + b"\0" for entry in sorted(entries)))


def _expected(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise SourceError(f"{key} is not a SHA-256 digest")
    return value


def _archive(url: str, cached: Path | None) -> bytes:
    if cached is not None and cached.exists():
        with cached.open("rb") as stream:
            content = stream.read(MAX_ARCHIVE + 1)
    else:
        with urllib.request.urlopen(url, timeout=120) as response:
            content = bytes(response.read(MAX_ARCHIVE + 1))
    if len(content) > MAX_ARCHIVE:
        raise SourceError("the source archive exceeds its size limit")
    return content


def _unpack(content: bytes, staging: Path) -> Path:
    staging.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as bundle:
            # The data filter refuses paths and links that leave the staging directory.
            bundle.extractall(staging, filter="data")
    except (tarfile.TarError, EOFError, zlib.error) as error:
        raise SourceError("the source archive is unsafe or unreadable") from error
    # The root directory's name follows the repository name, which can change.
    # Only one root may exist, so nothing unverified sits beside the checkout.
    roots = list(staging.iterdir())
    if len(roots) != 1 or roots[0].is_symlink() or not roots[0].is_dir():
        raise SourceError("the source archive must have one root directory")
    return roots[0]


def extract(source: dict[str, Any], staging: Path, cache: Path | None = None) -> Path:
    """Extract the source into ``staging`` and return its verified root directory.

    A cached archive is verified like a downloaded one.
    Only an archive whose tree matched is cached.
    """
    expected = _expected(source, "tree_sha256")
    cached = None if cache is None else cache / f"{expected}.tar.gz"
    content = _archive(source["url"], cached)
    root = _unpack(content, staging)
    actual = tree_digest(root)
    if actual != expected:
        raise SourceError(f"the source tree digest is {actual}")
    if cached is not None and not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=cached.parent) as temporary:
            download = Path(temporary) / "archive"
            download.write_bytes(content)
            download.replace(cached)
    return root


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
        return tree_digest(_unpack(_archive(source["url"], None), Path(temporary) / "source"))


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
        prepare(arguments.output, arguments.cache)
    except SourceError as error:
        print(f"prepare_funnel: {error}", file=sys.stderr)
        return 1
    if arguments.image:
        build(arguments.output, arguments.image, test=arguments.test)
    return 0


if __name__ == "__main__":
    sys.exit(main())
