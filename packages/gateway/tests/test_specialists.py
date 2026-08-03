# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Tests for the bounded OpenHands research-specialist catalog."""

from __future__ import annotations

from pathlib import Path

import pytest
from openhands.sdk.testing import TestLLM

from heartwood.gateway._specialists import (
    SpecialistAvailability,
    SpecialistCapability,
    SpecialistCatalog,
    SpecialistCatalogError,
    load_specialist_catalog,
    specialist_agent_factory,
)


def test_bundled_specialist_catalog_is_ordered_and_bounded() -> None:
    catalog = _catalog()

    assert [role.specialist_id for role in catalog.roles] == [
        "research-planner",
        "data-quality-reviewer",
        "cohort-feature-reviewer",
        "statistical-reviewer",
        "reproducibility-reviewer",
        "analysis-implementer",
    ]
    assert len(catalog.available_roles) == 5
    for role in catalog.available_roles:
        assert role.capability == SpecialistCapability.ADVISORY
        assert role.availability == SpecialistAvailability.AVAILABLE
        assert role.definition.model == "inherit"
        assert role.definition.permission_mode == "always_confirm"
        assert role.definition.tools == []
        assert role.definition.max_iteration_per_run is not None
        assert role.definition.max_budget_per_run is not None
        assert role.presentation_summary.startswith("Advisory · Uses the active model")

    implementer = catalog.role("analysis-implementer")
    assert implementer.availability == SpecialistAvailability.UNAVAILABLE
    assert implementer.capability == SpecialistCapability.PROJECT_ACTIONS
    assert implementer.definition.tools == ["terminal", "heartwood_project_file_editor"]
    assert implementer.unavailable_reason


def test_catalog_rejects_unknown_specialist_id() -> None:
    with pytest.raises(SpecialistCatalogError, match="unknown specialist"):
        _catalog().role("unknown-reviewer")


def test_catalog_rejects_an_empty_directory(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    with pytest.raises(SpecialistCatalogError, match="catalog is empty"):
        load_specialist_catalog(agents_dir, _skills_dir())


@pytest.mark.parametrize(
    ("specialist_id", "skill_names"),
    [
        ("research-planner", []),
        ("data-quality-reviewer", ["omop-cohort-summary"]),
        ("cohort-feature-reviewer", ["omop-cohort-summary"]),
        ("statistical-reviewer", ["baseline-model"]),
        (
            "reproducibility-reviewer",
            ["omop-cohort-summary", "baseline-model", "aggregate-export"],
        ),
    ],
)
def test_specialist_factory_injects_only_verified_skills(
    specialist_id: str,
    skill_names: list[str],
) -> None:
    role = _catalog().role(specialist_id)
    agent = specialist_agent_factory(role)(TestLLM.from_messages([]))

    assert agent.tools == []
    assert agent.agent_context is not None
    assert [skill.name for skill in agent.agent_context.skills] == skill_names
    assert agent.agent_context.load_user_skills is False
    assert agent.agent_context.load_public_skills is False
    assert agent.agent_context.load_project_skills is False


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("model: inherit", "model: openai/unreviewed", "inherit the parent model route"),
        (
            "permission_mode: always_confirm",
            "permission_mode: never_confirm",
            "must use always_confirm",
        ),
    ],
)
def test_catalog_rejects_role_boundary_widening(
    old: str,
    new: str,
    message: str,
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    source = _valid_definition().replace(old, new)
    (agents_dir / "bounded-reviewer.md").write_text(source, encoding="utf-8")

    with pytest.raises(SpecialistCatalogError, match=message):
        load_specialist_catalog(agents_dir, _skills_dir())


def test_catalog_rejects_unverified_skill(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    source = _valid_definition().replace("skills: []", "skills:\n  - unknown-skill")
    (agents_dir / "bounded-reviewer.md").write_text(source, encoding="utf-8")

    with pytest.raises(SpecialistCatalogError, match="unverified Skill"):
        load_specialist_catalog(agents_dir, _skills_dir())


@pytest.mark.parametrize(
    ("configuration", "message"),
    [
        (
            "hooks:\n"
            "  pre_tool_use:\n"
            "    - matcher: '*'\n"
            "      hooks:\n"
            "        - command: echo blocked\n",
            "cannot define hooks",
        ),
        (
            "mcp_config:\n  external:\n    command: unreviewed-mcp-server\n",
            "cannot define MCP servers",
        ),
        (
            "profile_store_dir: /tmp/unreviewed-profiles\n",
            "cannot select an external model profile store",
        ),
    ],
)
def test_catalog_rejects_external_execution_and_model_configuration(
    configuration: str,
    message: str,
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    source = _valid_definition().replace(
        "permission_mode: always_confirm\n",
        f"permission_mode: always_confirm\n{configuration}",
    )
    (agents_dir / "bounded-reviewer.md").write_text(source, encoding="utf-8")

    with pytest.raises(SpecialistCatalogError, match=message):
        load_specialist_catalog(agents_dir, _skills_dir())


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "description: Reviews supplied synthetic evidence.",
            "description: ''",
            "requires a description",
        ),
        (
            "max_iteration_per_run: 4\n",
            "",
            "requires an iteration limit",
        ),
        (
            "max_budget_per_run: 0.5\n",
            "",
            "requires a usage budget",
        ),
        (
            "heartwood:\n",
            "heartwood: invalid\nignored:\n",
            "requires heartwood catalog metadata",
        ),
        (
            "label: Bounded Reviewer",
            "label: ''",
            "requires a non-empty label",
        ),
        (
            "capability: advisory",
            "capability: 7",
            "capability must be one of",
        ),
        (
            "capability: advisory",
            "capability: unsupported",
            "capability must be one of",
        ),
        (
            "order: 1",
            "order: -1",
            "requires a non-negative integer order",
        ),
        (
            "availability: available",
            "availability: unavailable",
            "must explain why it is unavailable",
        ),
        (
            "availability: available\n",
            "availability: available\n  unavailable_reason: Not needed.\n",
            "cannot declare an unavailable reason",
        ),
        (
            "availability: available\n",
            "availability: unavailable\n  unavailable_reason: Tools are disabled.\n",
            "must declare the blocked tool capability",
        ),
        (
            "capability: advisory",
            "capability: project-actions",
            "must remain unavailable",
        ),
    ],
)
def test_catalog_rejects_incomplete_or_invalid_role_metadata(
    old: str,
    new: str,
    message: str,
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    source = _valid_definition().replace(old, new)
    (agents_dir / "bounded-reviewer.md").write_text(source, encoding="utf-8")

    with pytest.raises(SpecialistCatalogError, match=message):
        load_specialist_catalog(agents_dir, _skills_dir())


def test_catalog_rejects_duplicate_role_labels(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "bounded-reviewer.md").write_text(_valid_definition(), encoding="utf-8")
    second = (
        _valid_definition()
        .replace("bounded-reviewer", "second-reviewer")
        .replace(
            "order: 1",
            "order: 2",
        )
    )
    (agents_dir / "second-reviewer.md").write_text(second, encoding="utf-8")

    with pytest.raises(SpecialistCatalogError, match="labels must be unique"):
        load_specialist_catalog(agents_dir, _skills_dir())


def test_catalog_does_not_silently_skip_malformed_openhands_definition(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "bounded-reviewer.md").write_text(_valid_definition(), encoding="utf-8")
    (agents_dir / "malformed.md").write_text(
        "---\nname: malformed\npermission_mode: unsupported\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(SpecialistCatalogError, match="could not load every"):
        load_specialist_catalog(agents_dir, _skills_dir())


def test_unavailable_specialist_cannot_be_instantiated() -> None:
    role = _catalog().role("analysis-implementer")

    with pytest.raises(SpecialistCatalogError, match="specialist is unavailable"):
        specialist_agent_factory(role)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "tools: []",
            "tools:\n  - terminal",
            "advisory specialist bounded-reviewer cannot declare tools",
        ),
        (
            "capability: advisory\n  availability: available",
            "capability: project-actions\n  availability: unavailable\n"
            "  unavailable_reason: Tool execution is disabled.",
            "project-actions specialist bounded-reviewer must declare tools",
        ),
    ],
)
def test_catalog_rejects_inconsistent_capabilities_and_limits(
    old: str,
    new: str,
    message: str,
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    source = _valid_definition().replace(old, new)
    (agents_dir / "bounded-reviewer.md").write_text(source, encoding="utf-8")

    with pytest.raises(SpecialistCatalogError, match=message):
        load_specialist_catalog(agents_dir, _skills_dir())


def _valid_definition() -> str:
    return """---
name: bounded-reviewer
description: Reviews supplied synthetic evidence.
model: inherit
tools: []
skills: []
max_iteration_per_run: 4
max_budget_per_run: 0.5
permission_mode: always_confirm
heartwood:
  label: Bounded Reviewer
  capability: advisory
  availability: available
  order: 1
---

Review only the supplied evidence and report bounded findings.
"""


def _catalog() -> SpecialistCatalog:
    return load_specialist_catalog(_agents_dir(), _skills_dir())


def _agents_dir() -> Path:
    return _repository_root() / "agents" / "verified"


def _skills_dir() -> Path:
    return _repository_root() / "skills" / "verified"


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]
