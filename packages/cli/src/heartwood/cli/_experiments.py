# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Command-line presentation of project-owned experiment recording and exports."""

from __future__ import annotations

import argparse
import subprocess
import sys
from uuid import UUID, uuid4

from pydantic import ValidationError

from heartwood.gateway import ProjectContext, SessionGateway
from heartwood.gateway.experiments import ExperimentRecorder
from heartwood.schemas.experiments import ExperimentEnvironment


def configure_experiments(parser: argparse.ArgumentParser) -> None:
    """Attach script recording and project-wide inspection commands."""
    commands = parser.add_subparsers(dest="experiment_command", required=True)
    commands.add_parser(
        "environment", help="Print the isolated verification Python environment as JSON."
    )
    listing = commands.add_parser("list", help="Inspect this project's recorded experiments.")
    listing.add_argument(
        "--json", action="store_true", help="Print the shared project record schema."
    )
    commands.add_parser(
        "export", help="Write verified project experiment JSONL to standard output."
    )
    record = commands.add_parser(
        "record", help="Run a declared analysis script and record its outcome."
    )
    record.add_argument(
        "--input", action="append", default=[], help="Input file; repeat as needed."
    )
    record.add_argument(
        "--output", action="append", default=[], help="New output file; repeat as needed."
    )
    record.add_argument("--code", action="append", default=[], help="Additional code dependency.")
    record.add_argument(
        "--actor", default="researcher", help="Non-sensitive declared actor reference."
    )
    record.add_argument(
        "--run-id", type=UUID, help="Stable identity; reusing it never repeats execution."
    )
    record.add_argument("--runner", help="Another interpreter executable, such as bash or Rscript.")
    record.add_argument(
        "--environment-sha256", help="Declared environment digest for another interpreter."
    )
    record.add_argument("script", help="Project-relative script; defaults to Heartwood's Python.")
    record.add_argument(
        "arguments", nargs=argparse.REMAINDER, help="Arguments passed literally to the script."
    )
    for command in ("recover", "cancel"):
        operation = commands.add_parser(
            command,
            help=f"{command.capitalize()} an abandoned script record; does not stop processes.",
        )
        operation.add_argument("run_id", type=UUID)


def handle_experiments(args: argparse.Namespace, *, project: ProjectContext) -> int:
    """Use shared gateway services without opening a model conversation."""
    try:
        if args.experiment_command in {"list", "export", "environment"}:
            gateway = SessionGateway(project=project)
            try:
                if args.experiment_command == "environment":
                    print(gateway.verification_environment().model_dump_json(indent=2))
                elif args.experiment_command == "export":
                    sys.stdout.write(gateway.export_experiments().jsonl)
                else:
                    records = gateway.experiment_records()
                    if args.json:
                        print(records.model_dump_json(indent=2))
                    elif not records.runs:
                        print("No experiments recorded in this project.")
                    else:
                        for run in records.runs:
                            stage = run.definition.stage
                            label = (
                                stage.stage_id if stage is not None else run.definition.entry_point
                            )
                            print(f"{run.run_id}  {run.status}  attempt {run.attempt}  {label}")
                return 0
            finally:
                gateway.stop()
        recorder = ExperimentRecorder(project)
        if args.experiment_command in {"recover", "cancel"}:
            operation = (
                recorder.recover if args.experiment_command == "recover" else recorder.cancel
            )
            run = operation(args.run_id)
            print(f"{run.run_id}: {run.status}. No processes stopped or files removed.")
            return 0
        environment = (
            ExperimentEnvironment(
                kind="declared", source="declared", sha256=args.environment_sha256
            )
            if args.environment_sha256 is not None
            else None
        )
        identity = args.run_id or uuid4()
        print(
            f"Recording experiment {identity}. "
            "Fingerprinting files can take time; script output follows.",
            file=sys.stderr,
        )
        run = recorder.record_command(
            actor_ref=args.actor,
            entry_point=args.script,
            inputs=tuple(args.input),
            outputs=tuple(args.output),
            code=tuple(args.code),
            arguments=tuple(args.arguments),
            runner=args.runner,
            environment=environment,
            run_id=identity,
        )
        print(f"Experiment {run.run_id}: {run.status}.", file=sys.stderr)
        return 0
    except subprocess.CalledProcessError as error:
        print(
            f"Analysis exited with status {error.returncode}; its failure was recorded.",
            file=sys.stderr,
        )
        return error.returncode if error.returncode > 0 else 128 - error.returncode
    except OSError:
        print(
            "Analysis or record storage unavailable; inspect the experiment before retrying.",
            file=sys.stderr,
        )
        return 74
    except ValidationError:
        print(
            "Invalid experiment declaration. Check project-relative file paths, "
            "the actor reference, and any complete SHA-256 digest.",
            file=sys.stderr,
        )
        return 64
    except ValueError as error:
        print(f"Experiment unavailable: {error}", file=sys.stderr)
        return 64
