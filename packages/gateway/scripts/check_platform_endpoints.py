# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Check a platform's inference and MCP endpoints, or the in-process fakes, live."""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from heartwood.gateway.platform_conformance import (
    InferenceEndpoint,
    McpEndpoint,
    run_platform_conformance,
)
from heartwood.gateway.platform_fakes import running_platform_fakes
from heartwood.schemas import PlatformMcpServiceProfile


def main() -> None:
    """Print each capability's checks; credentials are read from named variables only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fakes", action="store_true", help="Check the deterministic fakes")
    parser.add_argument("--inference-url", help="OpenAI-compatible base URL, ending in /v1")
    parser.add_argument("--inference-key-env", help="Variable that holds the inference key")
    parser.add_argument("--model", help="Model for the Chat Completions check")
    parser.add_argument(
        "--mcp-profile",
        action="append",
        default=[],
        type=Path,
        help="heartwood.platform-mcp-service.v1 profile; repeat for each service",
    )
    parser.add_argument(
        "--mcp-token-env",
        action="append",
        default=[],
        help="Variable that holds the bearer token for the profile at the same position",
    )
    arguments = parser.parse_args()
    endpoint_options = (
        arguments.inference_url,
        arguments.inference_key_env,
        arguments.model,
        arguments.mcp_profile,
        arguments.mcp_token_env,
    )
    if arguments.fakes == any(endpoint_options):
        parser.error("give either --fakes or endpoint options")
    if arguments.mcp_token_env and len(arguments.mcp_token_env) != len(arguments.mcp_profile):
        parser.error("--mcp-token-env must be given once for each --mcp-profile")
    for name in ("httpx", "mcp", "fastmcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if arguments.fakes:
        # The fakes listen on loopback; a configured proxy must not receive those requests.
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        with running_platform_fakes() as fakes:
            results = run_platform_conformance(*fakes.endpoints())
    else:
        if arguments.inference_url is None and (
            arguments.inference_key_env or arguments.model or not arguments.mcp_profile
        ):
            parser.error("give --inference-url with inference options, or --mcp-profile")
        try:
            profiles = [
                PlatformMcpServiceProfile.model_validate_json(path.read_bytes())
                for path in arguments.mcp_profile
            ]
        except (OSError, ValueError):
            parser.exit(1, "An MCP service profile could not be read or failed validation.\n")
        tokens: list[str | None] = [_variable(parser, name) for name in arguments.mcp_token_env]
        tokens = tokens or [None] * len(profiles)
        key = arguments.inference_key_env
        try:
            inference = (
                None
                if arguments.inference_url is None
                else InferenceEndpoint(
                    arguments.inference_url,
                    None if key is None else _variable(parser, key),
                    arguments.model,
                )
            )
            services = tuple(
                McpEndpoint(profile, token) for profile, token in zip(profiles, tokens, strict=True)
            )
        except ValueError as error:
            parser.error(str(error))
        results = run_platform_conformance(inference, services)
    print(json.dumps({"results": [asdict(result) for result in results]}, indent=2))
    if any(result.status != "passed" for result in results):
        raise SystemExit(1)


def _variable(parser: argparse.ArgumentParser, name: str) -> str:
    value = os.environ.get(name)
    if not value:
        parser.error(f"variable {name} is not set")
    return value


if __name__ == "__main__":
    main()
