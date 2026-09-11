# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

import pytest
from pydantic import ValidationError

from heartwood.core_adapter.reproduction import ReproductionSpec, ReproductionWitness

_INPUTS = {
    "results/analysis.py": "print('synthetic computation')\n",
    "data.csv": "x,y\n1,2\n",
    "dictionary.json": '{"x":"feature","y":"outcome"}',
    "results/metrics.json": '{"mae":0}',
}
_OUTPUTS = {
    "results/reproduced/metrics.json": '{"mae":0}',
    "results/reproduced/predictions.csv": "prediction\n2\n",
}


class _PreparationOptions(TypedDict, total=False):
    command: str
    group_size: int
    destination_absent: bool
    changed: bool


def _spec(**changes: object) -> ReproductionSpec:
    return ReproductionSpec.model_validate(
        {
            "program": "results/analysis.py",
            "data": "data.csv",
            "directory": "results/reproduced",
            "protected_paths": tuple(_INPUTS),
            "output_names": ("metrics.json", "predictions.csv"),
            **changes,
        }
    )


def _prepared(
    *,
    command: str | None = None,
    group_size: int = 1,
    destination_absent: bool = True,
    changed: bool = False,
) -> ReproductionWitness | None:
    return ReproductionWitness.prepare(
        session_id="synthetic-session",
        run_id="synthetic-run",
        stage_id="verify",
        tool_call_id="rerun-action",
        spec=_spec(),
        command=command if command is not None else _spec().command,
        group_size=group_size,
        destination_absent=destination_absent,
        expected=_INPUTS,
        observed=_INPUTS if not changed else {**_INPUTS, "data.csv": "changed"},
    )


def _observe(
    witness: ReproductionWitness,
    *,
    tool_call_id: str = "rerun-action",
    approved: bool = True,
    exit_code: int = 0,
    protected: Mapping[str, str] = _INPUTS,
    outputs: Mapping[str, str] = _OUTPUTS,
) -> ReproductionWitness:
    return witness.observe(
        tool_call_id=tool_call_id,
        approved=approved,
        exit_code=exit_code,
        protected=protected,
        outputs=outputs,
    )


def test_preparation_is_not_execution_or_authorization() -> None:
    witness = _prepared()
    assert witness is not None
    assert witness.status == "prepared"
    assert not witness.verifies(protected=_INPUTS, outputs=_OUTPUTS)


@pytest.mark.parametrize("python", ["python", "../python", "", "/bin/python\n"])
def test_bound_python_must_be_an_absolute_safe_argument(python: str) -> None:
    with pytest.raises(ValidationError):
        _spec(python_executable=python)


def test_environment_guard_requires_a_protected_record_and_bound_python() -> None:
    with pytest.raises(ValidationError):
        _spec(required_environment="data.csv")
    with pytest.raises(ValidationError):
        _spec(python_executable="/opt/runtime/bin/python", required_environment="unbound.json")
    spec = _spec(python_executable="/opt/runtime/bin/python", required_environment="data.csv")
    assert spec.command == (
        "/opt/runtime/bin/python -I -m heartwood.gateway._environment_probe --require data.csv"
        " && /opt/runtime/bin/python -I results/analysis.py --data data.csv"
        " --output-dir results/reproduced"
    )
    assert not spec.matches_command(spec.command.replace(" && ", " ; "))


def test_success_round_trips_without_contents_and_requires_current_files() -> None:
    prepared = _prepared()
    assert prepared is not None
    witness = _observe(prepared)
    assert witness.status == "succeeded"
    encoded = witness.model_dump_json()
    assert all(content not in encoded for content in (*_INPUTS.values(), *_OUTPUTS.values()))
    restored = ReproductionWitness.model_validate_json(encoded)
    assert restored == witness
    assert restored.verifies(protected=_INPUTS, outputs=_OUTPUTS)
    assert not restored.verifies(protected={**_INPUTS, "data.csv": "changed"}, outputs=_OUTPUTS)
    assert not restored.verifies(protected=_INPUTS, outputs={})
    assert not restored.verifies(
        protected=_INPUTS, outputs={**_OUTPUTS, "results/reproduced/metrics.json": "changed"}
    )
    assert not restored.verifies(protected={}, outputs=_OUTPUTS)


@pytest.mark.parametrize(
    "changes",
    [
        {"group_size": 0},
        {"group_size": 2},
        {"destination_absent": False},
        {"changed": True},
        {"command": "true"},
        {"command": "cd results && python analysis.py --data ../data.csv --output-dir reproduced"},
    ],
)
def test_preparation_rejects_ambiguous_or_changed_execution(changes: _PreparationOptions) -> None:
    assert _prepared(**changes) is None


@pytest.mark.parametrize(
    "command",
    [
        "python results/analysis.py --data data.csv --output-dir results/reproduced; true",
        "python results/analysis.py --data data.csv --output-dir results/reproduced\n",
        "python results/analysis.py --data data.csv --output-dir results/reproduced # comment",
        "python results/analysis.py --data data.csv --output-dir results/reproduced &",
        "python results/analysis.py --data data.csv --output-dir results/reproduced > log",
        "python 'results/analysis.py' --data data.csv --output-dir results/reproduced",
        "python results/analysis.py --data data.csv --output-dir $OUTPUT",
    ],
)
def test_exact_invocation_disallows_shell_composition(command: str) -> None:
    assert not _spec().matches_command(command)


def test_paths_with_shell_metacharacters_are_quoted_as_data() -> None:
    spec = _spec(
        program="a folder/analysis.py",
        data="$(touch marker).csv",
        directory="results/$output",
        protected_paths=("a folder/analysis.py", "$(touch marker).csv"),
    )
    assert spec.command == (
        "python 'a folder/analysis.py' --data '$(touch marker).csv' --output-dir 'results/$output'"
    )
    assert spec.matches_command(spec.command)
    assert not spec.matches_command(
        spec.command.replace("'$(touch marker).csv'", "$(touch marker).csv")
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"program": "../analysis.py"},
        {"program": "-c", "protected_paths": ("-c", "data.csv")},
        {"data": "/data.csv"},
        {"data": "-secret.csv"},
        {"directory": ".heartwood/reproduced"},
        {"directory": "results/metrics.json/rerun"},
        {"directory": "data.csv"},
        {"directory": "results"},
        {"directory": "-output"},
        {"output_names": ("../metrics.json",)},
        {"output_names": (".git/config",)},
        {"output_names": ("metrics.json", "METRICS.json")},
        {"output_names": ("metrics.json", "metrics.json/child")},
        {"output_names": ()},
        {"protected_paths": ("data.csv", "data.csv")},
        {"protected_paths": (*_INPUTS, "DATA.csv")},
        {"protected_paths": (*_INPUTS, "results")},
        {"protected_paths": (*_INPUTS, "results/reproduced/unexpected.json")},
        {"protected_paths": ("results/analysis.py", "data.csv", ".heartwood/secret")},
        {"program": "data.csv"},
    ],
)
def test_spec_rejects_unsafe_or_overlapping_paths(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _spec(**changes)


def test_preparation_requires_every_protected_file() -> None:
    assert (
        ReproductionWitness.prepare(
            session_id="s",
            run_id="r",
            stage_id="verify",
            tool_call_id="a",
            spec=_spec(),
            command=_spec().command,
            group_size=1,
            destination_absent=True,
            expected={"data.csv": _INPUTS["data.csv"]},
            observed={"data.csv": _INPUTS["data.csv"]},
        )
        is None
    )


def test_failed_observation_cannot_be_repaired_by_later_matching_outputs() -> None:
    prepared = _prepared()
    assert prepared is not None
    failures = (
        _observe(prepared, tool_call_id="different-action"),
        _observe(prepared, approved=False),
        _observe(prepared, exit_code=1),
        _observe(prepared, protected={}),
        _observe(prepared, protected={**_INPUTS, "results/analysis.py": "pass"}),
        _observe(prepared, outputs={}),
        _observe(prepared, outputs={**_OUTPUTS, "results/reproduced/extra.csv": "extra"}),
    )
    for failed in failures:
        assert failed.status == "failed"
        assert failed.outputs == ()
        assert _observe(failed) == failed
        assert not failed.verifies(protected=_INPUTS, outputs=_OUTPUTS)


def test_captured_success_cannot_be_overwritten_by_later_observations() -> None:
    prepared = _prepared()
    assert prepared is not None
    witness = _observe(prepared)
    assert _observe(witness, outputs={}) == witness
    assert not witness.verifies(protected=_INPUTS, outputs={})


@pytest.mark.parametrize("change", ["duplicate", "missing", "wrong-output", "wrong-status"])
def test_malformed_serialized_evidence_is_rejected(change: str) -> None:
    prepared = _prepared()
    assert prepared is not None
    value = _observe(prepared).model_dump()
    if change == "duplicate":
        value["protected"] = (*value["protected"], value["protected"][0])
    elif change == "missing":
        value["protected"] = value["protected"][1:]
    elif change == "wrong-output":
        value["outputs"][0]["path"] = "different.json"
    else:
        value["status"] = "prepared"
    with pytest.raises(ValidationError):
        ReproductionWitness.model_validate(value)
