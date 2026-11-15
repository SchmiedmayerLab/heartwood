# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Check a captured platform response offline; never contact or invoke a service."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from heartwood.adapters.mcp_tasks_conformance import validate_mcp_task_exchange
from heartwood.adapters.service_conformance import (
    ServiceConformanceError,
    ServiceConformanceReport,
    validate_mcp_tools_list,
    validate_model_catalog,
    validate_platform_message,
)


def main() -> None:
    """Print checks and their limits without echoing potentially sensitive response values."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "kind", help="model-catalog, mcp-tools, mcp-tasks, or a draft *.schema.json filename"
    )
    parser.add_argument(
        "response",
        type=Path,
        help="Captured JSON result, or mcp-tasks transcript with server_capabilities and exchanges",
    )
    arguments = parser.parse_args()
    try:
        response = json.loads(arguments.response.read_bytes())
        if arguments.kind == "mcp-tasks":
            output = {"reports": [asdict(report) for report in _task_reports(response)]}
        elif arguments.kind == "model-catalog":
            output = asdict(validate_model_catalog(response))
        elif arguments.kind == "mcp-tools":
            output = asdict(validate_mcp_tools_list(response))
        else:
            output = asdict(validate_platform_message(arguments.kind, response))
    except (OSError, ValueError, RecursionError, ServiceConformanceError):
        parser.exit(1, "Response could not be read or failed contract validation.\n")
    print(json.dumps(output, indent=2))


def _task_reports(transcript: object) -> list[ServiceConformanceReport]:
    """Validate every captured exchange without reporting its names or payload values."""
    if not isinstance(transcript, dict):
        raise ValueError("invalid transcript")
    capabilities = transcript.get("server_capabilities")
    exchanges = transcript.get("exchanges")
    if not isinstance(capabilities, dict) or not isinstance(exchanges, list) or not exchanges:
        raise ValueError("invalid transcript")
    reports = []
    for exchange in exchanges:
        if (
            not isinstance(exchange, dict)
            or "request" not in exchange
            or "response" not in exchange
        ):
            raise ValueError("invalid exchange")
        reports.append(
            validate_mcp_task_exchange(
                exchange["request"], exchange["response"], server_capabilities=capabilities
            )
        )
    return reports


if __name__ == "__main__":
    main()
