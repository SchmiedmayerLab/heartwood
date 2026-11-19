# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""kubectl access to the cluster under test."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

CONTROL = "system:serviceaccount:heartwood-platform:heartwood-control"


@dataclass(frozen=True)
class Cluster:
    context: str

    def kubectl(
        self, *arguments: str, actor: str | None = None, document: dict[str, Any] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run kubectl, as a ServiceAccount when an actor is given."""
        command = ["kubectl", "--context", self.context]
        if actor is not None:
            command.append(f"--as={actor}")
        command.extend(arguments)
        return subprocess.run(
            command,
            input=None if document is None else json.dumps(document),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def apply(self, *documents: dict[str, Any]) -> None:
        """Apply objects as the cluster administrator."""
        items = {"apiVersion": "v1", "kind": "List", "items": list(documents)}
        result = self.kubectl("apply", "--server-side", "--filename", "-", document=items)
        assert result.returncode == 0, result.stderr
