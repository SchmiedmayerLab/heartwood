# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Command-line presentation of research MCP server management."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from heartwood.gateway import ProjectConfigError, SessionGateway
from heartwood.gateway._credentials import CredentialStoreError
from heartwood.gateway._mcp_policy import McpServerError
from heartwood.schemas import (
    McpServerAddRequest,
    McpServerEnabledRequest,
    McpServerResponse,
    McpServersResponse,
    McpServerTestRequest,
    McpServerTestResponse,
    McpServerToolsRequest,
    McpToolChoice,
)


def configure_services(services: argparse.ArgumentParser) -> None:
    """Attach research MCP server commands; each reads the deployment's destinations."""
    services.add_argument(
        "--deployment",
        type=Path,
        help="Absolute operator manifest whose MCP destinations and worker the servers use.",
    )
    commands = services.add_subparsers(dest="services_command", metavar="<services-command>")
    commands.add_parser("list", help="Show servers, selected tools and the enabled tool set.")
    add = commands.add_parser("add", help="Add a server at a deployment-approved origin.")
    add.add_argument("server_id")
    add.add_argument("url")
    add.add_argument("--label", required=True)
    _token_arguments(add)
    test = commands.add_parser("test", help="Connect to a server and list its tools.")
    test.add_argument("server_id")
    _token_arguments(test)
    select = commands.add_parser(
        "select", help="Select reviewed tool definitions; an empty list removes all."
    )
    select.add_argument("server_id")
    select.add_argument(
        "tools",
        nargs="*",
        metavar="TOOL=SHA256",
        type=_tool_choice,
        help="Tool name and the complete definition digest printed by `services test`.",
    )
    _token_arguments(select)
    for name, summary in (
        ("enable", "Offer the server's selected tools to new sessions."),
        ("disable", "Stop offering the server's tools; pending calls are refused."),
        ("disconnect", "Remove the server and forget its token."),
    ):
        commands.add_parser(name, help=summary).add_argument("server_id")


def _tool_choice(value: str) -> McpToolChoice:
    name, separator, digest = value.partition("=")
    try:
        if not separator:
            raise ValueError
        return McpToolChoice(name=name, sha256=digest.removeprefix("sha256:").lower())
    except (ValueError, ValidationError):
        raise argparse.ArgumentTypeError(
            "expected TOOL=SHA256 with the complete digest from `services test`"
        ) from None


def _token_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read the server's bearer token from standard input.",
    )
    parser.add_argument(
        "--remember",
        action="store_true",
        help="Keep the token in the system credential store when the deployment allows it.",
    )


def handle_services(
    parser: argparse.ArgumentParser, gateway: SessionGateway, args: argparse.Namespace
) -> int:
    """Run one research service command through the gateway's shared operations."""
    command = getattr(args, "services_command", None)
    token = None
    if getattr(args, "token_stdin", False):
        token = sys.stdin.readline().strip() or None
    remember = bool(getattr(args, "remember", False))
    try:
        if command == "list":
            print(format_mcp_servers(gateway.mcp_server_settings()))
        elif command == "add":
            print(
                format_mcp_servers(
                    gateway.add_mcp_server(
                        McpServerAddRequest(
                            server_id=args.server_id,
                            label=args.label,
                            url=args.url,
                            token=token,
                            remember=remember,
                        )
                    )
                )
            )
        elif command == "test":
            print(
                format_mcp_test(
                    gateway.test_mcp_server(
                        args.server_id, McpServerTestRequest(token=token, remember=remember)
                    )
                )
            )
        elif command == "select":
            print(
                format_mcp_servers(
                    gateway.select_mcp_tools(
                        args.server_id,
                        McpServerToolsRequest(
                            tools=list(args.tools), token=token, remember=remember
                        ),
                    )
                )
            )
        elif command in {"enable", "disable"}:
            print(
                format_mcp_servers(
                    gateway.set_mcp_server_enabled(
                        args.server_id, McpServerEnabledRequest(enabled=command == "enable")
                    )
                )
            )
        elif command == "disconnect":
            print(format_mcp_servers(gateway.disconnect_mcp_server(args.server_id)))
            print("Completed remote work is not undone.")
        else:
            parser.error("choose a services command")
    except ValidationError as error:
        details = "; ".join(str(item["msg"]) for item in error.errors(include_url=False))
        print(f"Research service request is invalid: {details}", file=sys.stderr)
        return 1
    except (McpServerError, CredentialStoreError, ProjectConfigError) as error:
        print(f"Research service unavailable: {error}", file=sys.stderr)
        return 1
    return 0


def format_mcp_servers(settings: McpServersResponse) -> str:
    """Return servers, their tools and the tool set enabled for new sessions."""
    lines = ["Heartwood research services", ""]
    if settings["availability"] == "unavailable":
        lines.extend((f"{settings['unavailable_code']}: {settings['unavailable_reason']}", ""))
    if not settings["destinations"]:
        lines.append("Approved origins: none")
    for item in settings["destinations"]:
        lines.append(
            f"Approved origin: {item['origin']}  service={item['service_id']}  "
            f"token variable={item['credential_binding']}"
        )
    if not settings["servers"]:
        return "\n".join((*lines, "No research services configured."))
    for server in settings["servers"]:
        lines.extend(("", *_server_lines(server)))
    return "\n".join(lines)


def _server_lines(server: McpServerResponse) -> list[str]:
    authentication = server["authentication"]
    expiry = authentication["expires_at"]
    lines = [
        f"{server['server_id']}  {server['label']}  "
        f"{'enabled' if server['enabled'] else 'disabled'}  destination={server['destination']}",
        f"    {server['url']}",
        f"    credential={authentication['status']}" + (f" until {expiry}" if expiry else ""),
    ]
    for tool in server["tools"]:
        lines.append(f"    {tool['name']}  status={tool['status']}  sha256={tool['sha256'][:12]}")
    enabled = ", ".join(server["enabled_tools"]) or "none"
    lines.append(f"    Enabled for new sessions: {enabled}")
    return lines


def format_mcp_test(result: McpServerTestResponse) -> str:
    """Return the tools a test listed; listing authorizes nothing."""
    lines = [*_server_lines(result["server"]), "", "Listed tools (not authorized by listing):"]
    for tool in result["tools"]:
        hints = tool["annotations"]
        flags = [
            "allowed" if tool["allowed"] else "not allowed by the deployment",
            "accepts operation binding"
            if tool["accepts_operation_binding"]
            else "no operation binding",
            f"read-only hint={hints['read_only']}",
        ]
        if tool["changed"]:
            flags.append("changed since review")
        lines.append(f"  {tool['name']}  {'; '.join(flags)}")
        lines.append(f"      select as {tool['name']}={tool['sha256']}")
        if tool["description"]:
            lines.append(f"      {tool['description']}")
        lines.append(f"      parameters: {tool['parameters']}")
    for name, capability in (("resources", result["resources"]), ("prompts", result["prompts"])):
        if capability["advertised"]:
            lines.append(f"The server advertises {name}; Heartwood does not use them.")
    return "\n".join(lines)
