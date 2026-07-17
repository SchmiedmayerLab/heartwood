# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verify that recommended Hugging Face model revisions still resolve exactly."""

from __future__ import annotations

import argparse
import json
import re
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SNAPSHOT_CATALOG = Path("images/generic/local-runtime/snapshots.toml")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ModelSourceVerificationError(RuntimeError):
    """Raised when a recommended model source cannot be verified."""


class ModelSourceUnavailableError(RuntimeError):
    """Raised when the provider cannot be reached after bounded retries."""


@dataclass(frozen=True)
class ModelSource:
    """One immutable recommended model source."""

    model_id: str
    repository: str
    revision: str

    @property
    def api_url(self) -> str:
        """Return the Hugging Face revision endpoint."""
        repository = urllib.parse.quote(self.repository, safe="/")
        revision = urllib.parse.quote(self.revision, safe="")
        return f"https://huggingface.co/api/models/{repository}/revision/{revision}"


JsonFetcher = Callable[[str, float], dict[str, Any]]


def load_model_sources(source_root: Path) -> tuple[ModelSource, ...]:
    """Load and validate every recommended model pin from the catalog."""
    catalog_path = source_root / _SNAPSHOT_CATALOG
    with catalog_path.open("rb") as file:
        catalog = tomllib.load(file)
    snapshots = catalog.get("snapshots")
    if not isinstance(snapshots, dict) or not snapshots:
        raise ModelSourceVerificationError("model snapshot catalog contains no snapshots")
    sources: list[ModelSource] = []
    for model_id, value in sorted(snapshots.items()):
        if not isinstance(model_id, str) or not isinstance(value, dict):
            raise ModelSourceVerificationError("model snapshot catalog is malformed")
        repository = value.get("source_repository")
        revision = value.get("source_revision")
        if not isinstance(repository, str) or repository.count("/") != 1:
            raise ModelSourceVerificationError(f"{model_id}: invalid Hugging Face repository")
        if not isinstance(revision, str) or _COMMIT_PATTERN.fullmatch(revision) is None:
            raise ModelSourceVerificationError(f"{model_id}: revision must be a commit SHA")
        sources.append(ModelSource(model_id, repository, revision))
    return tuple(sources)


def verify_model_source(source: ModelSource, *, fetch_json: JsonFetcher) -> None:
    """Require the remote repository and immutable commit to match the catalog."""
    payload = fetch_json(source.api_url, 20)
    repository = payload.get("id")
    if repository is None:
        repository = payload.get("modelId")
    revision = payload.get("sha")
    if repository != source.repository:
        raise ModelSourceVerificationError(
            f"{source.model_id}: expected repository {source.repository}, got {repository!r}"
        )
    if revision != source.revision:
        raise ModelSourceVerificationError(
            f"{source.model_id}: expected revision {source.revision}, got {revision!r}"
        )


def _fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "heartwood-release-verifier"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ModelSourceVerificationError("Hugging Face returned a non-object response")
            return payload
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code not in {408, 425, 429}:
                raise ModelSourceVerificationError(
                    f"Hugging Face rejected the immutable revision request with HTTP {error.code}"
                ) from error
            last_error = error
            if attempt < 2:
                time.sleep(attempt + 1)
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(attempt + 1)
    raise ModelSourceUnavailableError(f"Hugging Face is unavailable: {last_error}")


def main() -> int:
    """Verify all catalog model sources."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--allow-unavailable",
        action="store_true",
        help="warn instead of failing when the provider is temporarily unavailable",
    )
    args = parser.parse_args()
    try:
        sources = load_model_sources(args.source_root.resolve())
        for source in sources:
            try:
                verify_model_source(source, fetch_json=_fetch_json)
                print(f"Verified {source.repository}@{source.revision}")
            except ModelSourceUnavailableError as error:
                if not args.allow_unavailable:
                    raise
                print(f"WARNING: {source.model_id}: {error}")
    except (OSError, tomllib.TOMLDecodeError, ModelSourceVerificationError) as error:
        parser.error(str(error))
    except ModelSourceUnavailableError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
