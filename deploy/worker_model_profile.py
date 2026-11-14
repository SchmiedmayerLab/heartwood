# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Non-secret operator inputs for bounded native model qualification."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit


@dataclass(frozen=True)
class QualificationModelProfile:
    """Select one requested route without changing the provider key's authorization."""

    base_url: str
    model: str
    max_input_tokens: int = 32768
    max_output_tokens: int = 512
    max_iterations: int = 6
    deadline_seconds: int = 180

    def __post_init__(self) -> None:
        """Validate the route and finite qualification limits."""
        endpoint = urlsplit(self.base_url)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
            or endpoint.path != "/v1"
            or endpoint.port not in {None, 443}
            or any(char.isspace() for char in self.base_url)
        ):
            raise ValueError("Qualification requires an explicit HTTPS /v1 inference base")
        if not self.model.startswith("openai/") or not 8 < len(self.model) <= 256:
            raise ValueError("Qualification requires an explicit compatible model identifier")
        if any(not (char.isalnum() or char in "_./:-") for char in self.model):
            raise ValueError("Qualification model identifier contains unsupported characters")
        for value, lower, upper in (
            (self.max_input_tokens, 1024, 65536),
            (self.max_output_tokens, 64, 2048),
            (self.max_iterations, 3, 12),
            (self.deadline_seconds, 30, 600),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("Qualification limits are outside their bounded ranges")

    @property
    def endpoint(self) -> str:
        """Return the sole permitted inference operation."""
        return self.base_url + "/chat/completions"

    @classmethod
    def load(cls, path: Path) -> QualificationModelProfile:
        """Reject unknown fields, including inline credentials, before contacting Docker."""
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise ValueError("Qualification profile must be a small regular JSON file")
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError
            return cls(**value)
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError(
                "Qualification profile is invalid; credentials belong on stdin"
            ) from error

    def mapping(self) -> dict[str, str | int]:
        """Return public configuration suitable for qualification evidence."""
        return asdict(self)


def read_credential(stream: BinaryIO) -> str:
    """Read a single bounded secret only inside the trusted qualification controller."""
    value = stream.readline(16385)
    try:
        credential = value.removesuffix(b"\n").decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("Qualification credential is unavailable or invalid") from None
    if (
        not credential
        or len(value) > 16384
        or any(not 33 <= ord(char) <= 126 for char in credential)
    ):
        raise ValueError("Qualification credential is unavailable or invalid")
    return credential
