# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests of the Funnel source preparation: content verification and recorded patches."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
_FUNNEL = Path(__file__).resolve().parents[2] / "images" / "funnel"


def _load() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "prepare_funnel", _TOOLS / "prepare_funnel.py"
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


prepare_funnel = _load()

_TREE: dict[str, tuple[int | str, bytes]] = {
    "License.txt": (0o644, b"Synthetic license\n"),
    "bin/tool": (0o755, b"#!/bin/sh\n"),
    "main.go": (0o644, b"package main\n"),
    "alias.go": ("main.go", b""),
}


def _archive(
    tree: dict[str, tuple[int | str, bytes]], root: str = "funnel-abc", *, regenerated: bool = False
) -> bytes:
    """Build a revision archive; a regenerated one differs only in archive metadata."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as bundle:
        directories = sorted({str(Path(name).parent) for name in tree} - {"."})
        for name in ["", *directories]:
            entry = tarfile.TarInfo(f"{root}/{name}".rstrip("/"))
            entry.type, entry.mode = tarfile.DIRTYPE, 0o775 if regenerated else 0o755
            bundle.addfile(entry)
        for name, (mode, content) in sorted(tree.items(), reverse=regenerated):
            entry = tarfile.TarInfo(f"{root}/{name}")
            entry.mtime = 1 if regenerated else 0
            if isinstance(mode, str):
                entry.type, entry.linkname = tarfile.SYMTYPE, mode
                bundle.addfile(entry)
            else:
                entry.mode, entry.size = mode, len(content)
                bundle.addfile(entry, io.BytesIO(content))
    return gzip.compress(buffer.getvalue(), compresslevel=1 if regenerated else 9, mtime=0)


def _source(tmp_path: Path, archive: bytes, expected: str) -> dict[str, str]:
    path = tmp_path / f"archive-{hashlib.sha256(archive).hexdigest()[:8]}.tar.gz"
    path.write_bytes(archive)
    return {"url": path.as_uri(), "tree_sha256": expected}


def _expected(tmp_path: Path) -> str:
    staging = tmp_path / "reviewed"
    with tarfile.open(fileobj=io.BytesIO(_archive(_TREE)), mode="r:gz") as bundle:
        bundle.extractall(staging, filter="data")
    digest = prepare_funnel.tree_digest(staging / "funnel-abc")
    assert isinstance(digest, str)
    return digest


def test_a_regenerated_archive_of_the_same_tree_verifies(tmp_path: Path) -> None:
    expected = _expected(tmp_path)
    regenerated = _archive(_TREE, "renamed-abc", regenerated=True)
    assert regenerated != _archive(_TREE)
    cache = tmp_path / "cache"
    checkout = prepare_funnel.extract(
        _source(tmp_path, regenerated, expected), tmp_path / "a", cache
    )
    assert checkout.name == "checkout"
    assert (checkout / "alias.go").readlink() == Path("main.go")
    # The verified archive is cached and verified again when it is read from the cache.
    assert (cache / f"{expected}.tar.gz").read_bytes() == regenerated
    source = {"url": "file:///nonexistent", "tree_sha256": expected}
    assert prepare_funnel.extract(source, tmp_path / "b", cache).name == "checkout"


@pytest.mark.parametrize("change", ["content", "mode", "link", "added", "removed", "second-root"])
def test_changed_source_content_fails_closed(tmp_path: Path, change: str) -> None:
    expected = _expected(tmp_path)
    tree = dict(_TREE)
    if change == "content":
        tree["main.go"] = (0o644, b"package other\n")
    elif change == "mode":
        tree["main.go"] = (0o755, tree["main.go"][1])
    elif change == "link":
        tree["alias.go"] = ("License.txt", b"")
    elif change == "added":
        tree["extra.go"] = (0o644, b"")
    elif change == "removed":
        del tree["bin/tool"]
    archive = _archive(tree)
    if change == "second-root":
        buffer = io.BytesIO(gzip.decompress(archive))
        with tarfile.open(fileobj=buffer, mode="a") as bundle:
            entry = tarfile.TarInfo("beside/file")
            entry.size = 1
            bundle.addfile(entry, io.BytesIO(b"x"))
        archive = gzip.compress(buffer.getvalue())
    cache = tmp_path / "cache"
    with pytest.raises(prepare_funnel.SourceError):
        prepare_funnel.extract(_source(tmp_path, archive, expected), tmp_path / "x", cache)
    assert not any(cache.iterdir())


def test_an_archive_that_leaves_its_directory_is_refused(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        entry = tarfile.TarInfo("funnel-abc/../../escape")
        entry.size = 1
        bundle.addfile(entry, io.BytesIO(b"x"))
    archive = gzip.compress(buffer.getvalue())
    with pytest.raises(prepare_funnel.SourceError, match="unsafe"):
        prepare_funnel.extract(_source(tmp_path, archive, "0" * 64), tmp_path / "x")
    assert not (tmp_path / "escape").exists()


def _patch(name: str, old: str, new: str) -> bytes:
    return (
        f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-{old}\n+{new}\n"
    ).encode()


def _manifest(
    tmp_path: Path, patches: list[tuple[str, bytes, str | None]], patched: str | None = None
) -> Path:
    directory = tmp_path / "recorded"
    directory.mkdir()
    entries = []
    for name, content, digest in patches:
        (directory / name).write_bytes(content)
        entries.append({"path": name, "sha256": digest or hashlib.sha256(content).hexdigest()})
    source = _source(tmp_path, _archive(_TREE), _expected(tmp_path))
    source["patched_tree_sha256"] = patched or "0" * 64
    manifest = directory / "funnel.json"
    manifest.write_text(json.dumps({"source": source, "patches": entries}))
    return manifest


def _patched_digest(tmp_path: Path, content: bytes) -> str:
    tree = dict(_TREE)
    tree["main.go"] = (0o644, content)
    staging = tmp_path / "patched"
    with tarfile.open(fileobj=io.BytesIO(_archive(tree)), mode="r:gz") as bundle:
        bundle.extractall(staging, filter="data")
    digest = prepare_funnel.tree_digest(staging / "funnel-abc")
    assert isinstance(digest, str)
    return digest


def test_patches_apply_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _patch("main.go", "package main", "package first")
    second = _patch("main.go", "package first", "package second")
    patched = _patched_digest(tmp_path, b"package second\n")
    manifest = _manifest(
        tmp_path, [("0001.patch", first, None), ("0002.patch", second, None)], patched
    )
    # Neither the caller's repository nor its git configuration takes part.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    config = tmp_path / "gitconfig"
    config.write_text("[apply]\n\tignoreWhitespace = change\n\twhitespace = fix\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    target = prepare_funnel.prepare(tmp_path / "out", manifest=manifest)
    assert (target / "main.go").read_text() == "package second\n"


def test_a_patch_that_matches_only_without_whitespace_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "gitconfig"
    config.write_text("[apply]\n\tignoreWhitespace = change\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    patch = _patch("main.go", "package  main", "package first")
    manifest = _manifest(tmp_path, [("0001.patch", patch, None)])
    with pytest.raises(prepare_funnel.SourceError, match="does not apply"):
        prepare_funnel.prepare(tmp_path / "out", manifest=manifest)


def test_a_patched_tree_with_another_digest_is_refused(tmp_path: Path) -> None:
    patch = _patch("main.go", "package main", "package first")
    manifest = _manifest(tmp_path, [("0001.patch", patch, None)])
    with pytest.raises(prepare_funnel.SourceError, match="patched tree digest"):
        prepare_funnel.prepare(tmp_path / "out", manifest=manifest)
    assert not (tmp_path / "out" / "funnel").exists()


def test_a_non_empty_output_is_refused(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, [])
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "other").write_text("")
    with pytest.raises(prepare_funnel.SourceError, match="not empty"):
        prepare_funnel.prepare(tmp_path / "out", manifest=manifest)


def test_a_tampered_cached_archive_is_refused(tmp_path: Path) -> None:
    expected = _expected(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    tree = dict(_TREE)
    tree["main.go"] = (0o644, b"package tampered\n")
    (cache / f"{expected}.tar.gz").write_bytes(_archive(tree))
    source = {"url": "file:///nonexistent", "tree_sha256": expected}
    with pytest.raises(prepare_funnel.SourceError, match="tree_sha256 is"):
        prepare_funnel.extract(source, tmp_path / "x", cache)


def test_an_oversized_archive_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _archive(_TREE)
    monkeypatch.setattr(prepare_funnel, "MAX_ARCHIVE", len(archive) - 1)
    with pytest.raises(prepare_funnel.SourceError, match="size limit"):
        prepare_funnel.extract(_source(tmp_path, archive, "0" * 64), tmp_path / "x")


def test_a_root_that_is_a_link_is_refused(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        entry = tarfile.TarInfo("funnel-abc")
        entry.type, entry.linkname = tarfile.SYMTYPE, "."
        bundle.addfile(entry)
    archive = gzip.compress(buffer.getvalue())
    with pytest.raises(prepare_funnel.SourceError, match="one root"):
        prepare_funnel.extract(_source(tmp_path, archive, "0" * 64), tmp_path / "x")


@pytest.mark.parametrize("target", ["./main.go", "main.go/"])
def test_link_targets_are_compared_as_written(tmp_path: Path, target: str) -> None:
    trees = []
    for name in ("plain", "other"):
        root = tmp_path / name
        root.mkdir()
        (root / "main.go").write_text("package main\n")
        (root / "alias.go").symlink_to("main.go" if name == "plain" else target)
        trees.append(prepare_funnel.tree_digest(root))
    assert trees[0] != trees[1]


def test_a_special_file_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    os.mkfifo(root / "pipe")
    with pytest.raises(prepare_funnel.SourceError, match="special file"):
        prepare_funnel.tree_digest(root)


def test_a_patch_with_another_digest_is_refused(tmp_path: Path) -> None:
    patch = _patch("main.go", "package main", "package first")
    manifest = _manifest(tmp_path, [("0001.patch", patch, "0" * 64)])
    with pytest.raises(prepare_funnel.SourceError, match="recorded digest"):
        prepare_funnel.prepare(tmp_path / "out", manifest=manifest)
    assert not (tmp_path / "out" / "funnel").exists()


def test_a_patch_that_does_not_apply_is_refused(tmp_path: Path) -> None:
    patch = _patch("main.go", "package missing", "package first")
    manifest = _manifest(tmp_path, [("0001.patch", patch, None)])
    with pytest.raises(prepare_funnel.SourceError, match="does not apply"):
        prepare_funnel.prepare(tmp_path / "out", manifest=manifest)


def test_the_recorded_patches_match_their_digests() -> None:
    manifest = json.loads((_FUNNEL / "funnel.json").read_text())
    assert [patch["sha256"] for patch in manifest["patches"]] == [
        "96792eb97cbfc9c5d59afca8cea5591e7082ac0297f5a3a72e9794fd69033e47",
        "c3ae111d128629d2b51204d35cc3e71fbc1997b9f4246fa4e07f02a4c21a9af5",
        "1bc477092afef735d14813b5423fe5eabfb39f8794b3573d0137ea7af30aa6ab",
    ]
    for patch in manifest["patches"]:
        content = (_FUNNEL / patch["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == patch["sha256"]
        assert patch["purpose"]
        assert (patch["upstream"] or patch["related"]).startswith("https://")
        assert patch["removal"]
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["source"]["revision"])
    assert manifest["source"]["url"].endswith(manifest["source"]["revision"])


def test_the_dockerfile_uses_the_recorded_images_by_digest() -> None:
    manifest = json.loads((_FUNNEL / "funnel.json").read_text())
    dockerfile = (_FUNNEL / "Dockerfile").read_text()
    # A syntax line would pull a frontend image by tag.
    assert "# syntax=" not in dockerfile
    stages = re.findall(r"^FROM (\S+)", dockerfile, re.MULTILINE)
    external = [stage for stage in stages if stage != "build"]
    assert external == [manifest["images"]["go"], manifest["images"]["base"]]
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in external)
