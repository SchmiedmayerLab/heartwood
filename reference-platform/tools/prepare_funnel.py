# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Prepare the build context of the Funnel image from the pinned source and its patches.

The source is the revision archive that ``images/funnel/funnel.json`` names. GitHub
generates revision archives on request and does not keep their bytes stable, so the
manifest records the digest of the extracted tree, ``tree_sha256``, and the archive's
own bytes are not trusted. Each patch is verified by its SHA-256 and applied in order
without fuzz. The patched tree is written to ``OUTPUT/funnel``, the build context of
``images/funnel/Dockerfile``.

With ``--image TAG`` the image is built from the prepared context, after the tests of
the patched packages when ``--test`` is given.

Usage: prepare_funnel.py OUTPUT [--cache DIRECTORY] [--image TAG [--test]]
       prepare_funnel.py OUTPUT --print-tree-digest
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
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


class SourceError(Exception):
    """The source or a patch does not match the manifest."""


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def tree_digest(root: Path) -> str:
    """Identify an extracted tree by its paths, kinds, executable bits, contents and links.

    Archive metadata such as compression, timestamps and ownership is excluded, so a
    regenerated archive of the same revision keeps its digest. The records match those of
    ``_tree_digest`` in ``deploy/build-worker-candidate.py``.
    """
    entries: list[tuple[bytes, bytes, bytes]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                path = os.fsencode(Path(child.path).relative_to(root).as_posix())
                if child.is_symlink():
                    entries.append((path, b"symlink", os.fsencode(Path(child.path).readlink())))
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


def _archive(source: dict[str, Any], cache: Path | None) -> bytes:
    cached = None if cache is None else cache / f"{source['tree_sha256']}.tar.gz"
    if cached is not None and cached.exists():
        with cached.open("rb") as stream:
            return stream.read(MAX_ARCHIVE + 1)
    with urllib.request.urlopen(source["url"], timeout=120) as response:
        return bytes(response.read(MAX_ARCHIVE + 1))


def extract(source: dict[str, Any], staging: Path, cache: Path | None = None) -> Path:
    """Extract the source into ``staging`` and return its verified root directory.

    A cached archive is verified like a downloaded one, and only an archive whose tree
    matched is cached.
    """
    content = _archive(source, cache)
    if len(content) > MAX_ARCHIVE:
        raise SourceError("the source archive exceeds its size limit")
    staging.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as bundle:
            # The data filter refuses paths and links that leave the staging directory.
            bundle.extractall(staging, filter="data")
    except (tarfile.TarError, EOFError, zlib.error) as error:
        raise SourceError("the source archive is unsafe or unreadable") from error
    # The root directory's name follows the repository name, which can change. Only one
    # root may exist, so nothing unverified sits beside the checkout.
    roots = list(staging.iterdir())
    if len(roots) != 1 or roots[0].is_symlink() or not roots[0].is_dir():
        raise SourceError("the source archive must have one root directory")
    actual = tree_digest(roots[0])
    if actual != source["tree_sha256"]:
        raise SourceError(f"the source tree digest is {actual}")
    if cache is not None:
        cached = cache / f"{source['tree_sha256']}.tar.gz"
        if not cached.exists():
            cache.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=cache) as temporary:
                download = Path(temporary) / "archive"
                download.write_bytes(content)
                download.replace(cached)
    return roots[0]


def apply_patches(checkout: Path, patches: list[dict[str, str]], directory: Path) -> None:
    """Apply each patch after verifying its digest; any rejected hunk fails the build."""
    for patch in patches:
        path = directory / patch["path"]
        content = path.read_bytes()
        if _digest(content) != patch["sha256"]:
            raise SourceError(f"{patch['path']} does not match its recorded digest")
        # The checkout is not a repository, and git must not find an enclosing one.
        environment = {**os.environ, "GIT_CEILING_DIRECTORIES": str(checkout.parent)}
        for check in (["--check"], []):
            result = subprocess.run(
                ["git", "apply", *check, "-"],
                cwd=checkout,
                input=content,
                capture_output=True,
                env=environment,
                check=False,
            )
            if result.returncode != 0:
                message = result.stderr.decode(errors="replace").strip()
                raise SourceError(f"{patch['path']} does not apply: {message}")


def prepare(output: Path, cache: Path | None = None, manifest: Path = MANIFEST) -> Path:
    """Write the patched source to ``output/funnel`` and return that directory."""
    recorded = json.loads(manifest.read_text(encoding="utf-8"))
    target = output / "funnel"
    if target.exists():
        raise SourceError(f"{target} exists")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="funnel-source-", dir=output) as temporary:
        checkout = extract(recorded["source"], Path(temporary) / "source", cache)
        apply_patches(checkout, recorded["patches"], manifest.parent)
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


def main() -> int:
    """Prepare the build context named on the command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--image", help="build the image under this tag")
    parser.add_argument("--test", action="store_true", help="run the patch tests before")
    parser.add_argument(
        "--print-tree-digest",
        action="store_true",
        help="print the tree digest of the downloaded source instead of verifying it",
    )
    arguments = parser.parse_args()
    if arguments.print_tree_digest:
        source = json.loads(MANIFEST.read_text(encoding="utf-8"))["source"]
        with TemporaryDirectory() as temporary:
            staging = Path(temporary)
            with urllib.request.urlopen(source["url"], timeout=120) as response:
                content = response.read(MAX_ARCHIVE + 1)
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as bundle:
                bundle.extractall(staging, filter="data")
            (root,) = staging.iterdir()
            print(tree_digest(root))
        return 0
    try:
        prepare(arguments.output, arguments.cache)
    except SourceError as error:
        print(f"prepare_funnel: {error}", file=sys.stderr)
        return 1
    if arguments.image:
        build(arguments.output, arguments.image, test=arguments.test)
    return 0


if __name__ == "__main__":
    sys.exit(main())
