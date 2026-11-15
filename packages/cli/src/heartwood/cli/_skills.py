# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Command-line presentation of verified Agent Skill browsing and installation."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from heartwood.cli._interactive import InteractionActivity
from heartwood.cli._progress import _format_transfer_bytes, _run_with_progress
from heartwood.gateway import SessionGateway, SkillSettingsError
from heartwood.schemas import SkillSettingsResponse, SkillSummaryResponse


def _sha256_argument(value: str) -> str:
    digest = value.removeprefix("sha256:").lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise argparse.ArgumentTypeError("expected a complete SHA-256 digest")
    return digest


def configure_skills(skills: argparse.ArgumentParser) -> None:
    """Attach Skill browsing, review, and installation commands."""
    skill_subparsers = skills.add_subparsers(dest="skills_command", metavar="<skills-command>")
    skill_subparsers.add_parser("list", help="List available and active Skills.")
    refresh_skills = skill_subparsers.add_parser(
        "refresh", help="Refresh deployment-approved signed Skill sources."
    )
    refresh_skills.add_argument("--source", dest="source_id")
    inspect = skill_subparsers.add_parser("inspect", help="Review one Skill from a signed source.")
    inspect.add_argument("name")
    inspect.add_argument("--source", dest="source_id")
    install = skill_subparsers.add_parser(
        "install", help="Install one signed Skill after explicit review."
    )
    install.add_argument("name")
    install.add_argument("--source", dest="source_id")
    install.add_argument(
        "--approve",
        action="store_true",
        help="Approve the digest supplied with --expected-tree-sha256.",
    )
    install.add_argument(
        "--expected-tree-sha256",
        type=_sha256_argument,
        metavar="SHA256",
        help="Exact content digest shown by `skills inspect`; required with --approve.",
    )
    inspect_local = skill_subparsers.add_parser(
        "inspect-local", help="Inspect an advanced local, unreviewed Agent Skill."
    )
    inspect_local.add_argument("source", type=Path)
    install_local = skill_subparsers.add_parser(
        "install-local", help="Install an advanced local, unreviewed Agent Skill."
    )
    install_local.add_argument("source", type=Path)
    install_local.add_argument(
        "--approve",
        action="store_true",
        help="Approve the digest supplied with --expected-tree-sha256.",
    )
    install_local.add_argument(
        "--expected-tree-sha256",
        type=_sha256_argument,
        metavar="SHA256",
        help="Exact content digest shown by `skills inspect-local`; required with --approve.",
    )
    remove_skill = skill_subparsers.add_parser("remove", help="Remove an installed extension.")
    remove_skill.add_argument("name")


def _handle_skills(
    parser: argparse.ArgumentParser,
    gateway: SessionGateway,
    args: argparse.Namespace,
) -> int:
    command = getattr(args, "skills_command", None)
    try:
        if command == "list":
            print(_format_skill_settings(gateway.skill_settings()))
            return 0
        if command == "refresh":
            settings = _run_with_progress(
                lambda: gateway.refresh_skills(args.source_id),
                activity=InteractionActivity(
                    label="Refreshing verified Skills",
                    waiting_label="Still verifying Skill sources",
                    guidance="Network and offline source response times can vary.",
                ),
            )
            print(_format_skill_settings(settings))
            return 0
        if command == "inspect":
            summary = _run_with_progress(
                lambda: gateway.inspect_skill(args.name, source_id=args.source_id),
                activity=InteractionActivity(
                    label="Verifying the Skill catalog",
                    waiting_label="Still verifying the Skill catalog",
                    guidance="Heartwood is checking signed metadata and immutable digests.",
                ),
            )
            print(_format_skill_summary(summary))
            return 0
        if command == "install":
            expected_tree_sha256 = args.expected_tree_sha256
            if args.approve:
                if expected_tree_sha256 is None:
                    parser.error(
                        "--approve requires --expected-tree-sha256 with the digest shown by "
                        "`heartwood skills inspect`"
                    )
            else:
                if expected_tree_sha256 is not None:
                    parser.error("--expected-tree-sha256 requires --approve")
                summary = _run_with_progress(
                    lambda: gateway.inspect_skill(args.name, source_id=args.source_id),
                    activity=InteractionActivity(
                        label="Verifying the Skill catalog",
                        waiting_label="Still verifying the Skill catalog",
                        guidance="Heartwood is checking signed metadata and immutable digests.",
                    ),
                )
                expected_tree_sha256 = _confirm_skill_installation(
                    parser,
                    summary,
                    automation_command=(
                        "heartwood skills install "
                        f"{shlex.quote(args.name)}"
                        + (
                            f" --source {shlex.quote(args.source_id)}"
                            if args.source_id is not None
                            else ""
                        )
                        + " --approve --expected-tree-sha256 "
                        f"sha256:{summary['tree_sha256']}"
                    ),
                )
                if expected_tree_sha256 is None:
                    return 1
            settings = _run_with_progress(
                lambda: gateway.install_skill(
                    args.name,
                    source_id=args.source_id,
                    expected_tree_sha256=expected_tree_sha256,
                    approved=True,
                ),
                activity=InteractionActivity(
                    label="Installing the verified Skill",
                    waiting_label="Still installing the verified Skill",
                    guidance="Large Skill packages or remote sources can take additional time.",
                ),
            )
            print(_format_skill_settings(settings))
            return 0
        if command == "inspect-local":
            print(_format_skill_summary(gateway.inspect_local_skill(args.source)))
            return 0
        if command == "install-local":
            expected_tree_sha256 = args.expected_tree_sha256
            if args.approve:
                if expected_tree_sha256 is None:
                    parser.error(
                        "--approve requires --expected-tree-sha256 with the digest shown by "
                        "`heartwood skills inspect-local`"
                    )
            else:
                if expected_tree_sha256 is not None:
                    parser.error("--expected-tree-sha256 requires --approve")
                summary = gateway.inspect_local_skill(args.source)
                expected_tree_sha256 = _confirm_skill_installation(
                    parser,
                    summary,
                    automation_command=(
                        "heartwood skills install-local "
                        f"{shlex.quote(str(args.source))} --approve --expected-tree-sha256 "
                        f"sha256:{summary['tree_sha256']}"
                    ),
                )
                if expected_tree_sha256 is None:
                    return 1
            print(
                _format_skill_settings(
                    gateway.install_local_skill(
                        args.source,
                        expected_tree_sha256=expected_tree_sha256,
                        approved=True,
                    )
                )
            )
            return 0
        if command == "remove":
            print(_format_skill_settings(gateway.remove_skill(args.name)))
            return 0
    except SkillSettingsError as error:
        parser.error(str(error))
    parser.parse_args(["skills", "--help"])
    return 0


def _format_skill_settings(settings: SkillSettingsResponse) -> str:
    lines = ["Heartwood Skills", ""]
    skills = settings["skills"]
    if not skills:
        return "\n".join((*lines, "No Skills available."))
    for item in skills:
        source = item["source_id"] if item["source"] == "catalog" else item["source"]
        lines.append(
            f"{item['name']}  version={item['version']}  status={item['status']}  source={source}"
        )
        lines.append(f"    {item['description']}")
        if item["compatibility_reason"] is not None:
            lines.append(f"    {item['compatibility_reason']}")
    return "\n".join(lines)


def _format_skill_summary(summary: SkillSummaryResponse) -> str:
    tool_text = ", ".join(summary["declared_tools"])
    review = (
        "Repository reviewed"
        if summary["review"] == "repository-reviewed"
        else "Local and unreviewed"
    )
    controlled_data = "Deployment approved" if summary["controlled_data_ready"] else "Not approved"
    lines = [
        f"Skill: {summary['name']} {summary['version']}",
        f"Review: {review}",
        f"Source: {summary['source_id']}",
        f"Digest: sha256:{summary['tree_sha256']}",
        f"Tools: {tool_text or 'None declared'}",
        f"Network: {'required' if summary['requires_network'] else 'disabled'}",
        f"Data access: {summary['data_access_summary']}",
        f"Dataset types: {', '.join(summary['dataset_types']) or 'Not declared'}",
        f"Controlled data: {controlled_data}",
        f"Permissions: {summary['approval_summary']}",
    ]
    if summary["archive_size"] is not None:
        lines.insert(4, f"Download: {_format_transfer_bytes(summary['archive_size'])}")
    if summary["revocation_reason"] is not None:
        lines.append(f"Revoked: {summary['revocation_reason']}")
    if summary["compatibility_reason"] is not None:
        lines.append(f"Compatibility: {summary['compatibility_reason']}")
    return "\n".join(lines)


def _confirm_skill_installation(
    parser: argparse.ArgumentParser,
    summary: SkillSummaryResponse,
    *,
    automation_command: str,
) -> str | None:
    """Show one exact revision and obtain interactive or digest-bound approval."""
    print(_format_skill_summary(summary))
    if not sys.stdin.isatty():
        parser.error(
            "interactive approval is unavailable; rerun with the exact reviewed digest:\n"
            f"  {automation_command}"
        )
    approved = input("Install this exact Skill revision? [y/N]: ").strip().lower() == "y"
    if not approved:
        print("Skill installation canceled.")
        return None
    return summary["tree_sha256"]
