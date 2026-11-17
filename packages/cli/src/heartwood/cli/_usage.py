# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Command-line presentation of the content-free project usage summary."""

from __future__ import annotations

import argparse
import sys

from heartwood.gateway import ProjectContext, ProjectStateError, SessionGateway
from heartwood.schemas.usage import ProjectUsageSummary


def configure_usage(parser: argparse.ArgumentParser) -> None:
    """Attach the read-only usage summary command."""
    commands = parser.add_subparsers(dest="usage_command", required=True)
    summary = commands.add_parser(
        "summary", help="Summarize counts and outcomes without project or session content."
    )
    summary.add_argument(
        "--json", action="store_true", help="Print the published usage summary schema."
    )


def handle_usage(args: argparse.Namespace, *, project: ProjectContext) -> int:
    """Read verified records without opening a model conversation."""
    gateway = SessionGateway(project=project)
    try:
        summary = gateway.usage_summary()
    except ProjectStateError as error:
        print(f"Usage summary unavailable: {error}", file=sys.stderr)
        return 1
    finally:
        gateway.stop()
    print(summary.model_dump_json(indent=2) if args.json else _format_summary(summary))
    return 0 if summary.counts is not None else 1


def _format_summary(summary: ProjectUsageSummary) -> str:
    lines = [
        "Heartwood usage summary",
        f"  Summary: {summary.summary_id}",
        f"  Release: {summary.release_version}",
        f"  Platform: {summary.platform_id}",
    ]
    counts = summary.counts
    if counts is None:
        lines.append(
            "  Audit history: incomplete; a session has an interrupted commit that Heartwood "
            "completes when the session is next opened or verified. No counts are reported"
            if summary.audit_verification == "incomplete"
            else "  Audit history: verification failed; no counts are reported"
        )
        return "\n".join(lines)
    workflows = ", ".join(
        f"{phase} {count}" for phase, count in counts.workflow_runs.model_dump().items()
    )
    experiments = ", ".join(
        f"{status} {count}" for status, count in counts.experiment_runs.model_dump().items()
    )
    reproductions = counts.reproductions
    lines.extend(
        (
            "  Audit history: verified",
            f"  Sessions: {counts.sessions}",
            f"  Turns: {counts.turns}",
            f"  Action sets: {counts.action_sets.allowed} allowed, "
            f"{counts.action_sets.rejected} rejected",
            f"  Workflow runs: {workflows}",
            f"  Reproductions: {reproductions.succeeded} succeeded, {reproductions.failed} "
            f"failed, {reproductions.unsettled} unsettled",
            f"  Experiment runs: {experiments}",
        )
    )
    return "\n".join(lines)
