# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Validated research-specialist catalog over OpenHands agent definitions."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from openhands.sdk import LLM, Agent
from openhands.sdk.context import AgentContext
from openhands.sdk.skills import Skill, load_skills_from_dir
from openhands.sdk.subagent import (
    AgentDefinition,
    agent_definition_to_factory,
    load_agents_from_dir,
)


class SpecialistCatalogError(ValueError):
    """Raised when a bundled specialist violates the Heartwood contract."""


class SpecialistAvailability(StrEnum):
    """Whether a specialist can be delegated work in this release."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class SpecialistCapability(StrEnum):
    """The strongest capability declared by one specialist."""

    ADVISORY = "advisory"
    PROJECT_ACTIONS = "project-actions"


@dataclass(frozen=True, slots=True)
class SpecialistRole:
    """One trusted OpenHands specialist plus Heartwood presentation metadata."""

    definition: AgentDefinition
    label: str
    capability: SpecialistCapability
    availability: SpecialistAvailability
    unavailable_reason: str | None
    order: int
    verified_skills: tuple[Skill, ...]

    @property
    def specialist_id(self) -> str:
        """Return the OpenHands registration identifier."""
        return self.definition.name

    @property
    def presentation_summary(self) -> str:
        """Return the gateway-owned summary shared by every interface."""
        capability = (
            "Advisory" if self.capability == SpecialistCapability.ADVISORY else "Project actions"
        )
        return (
            f"{capability} · Uses the active model · "
            f"Up to {self.definition.max_iteration_per_run} steps"
        )

    def safe_dict(self) -> dict[str, object]:
        """Return deterministic non-secret catalog metadata for interfaces."""
        return {
            "specialist_id": self.specialist_id,
            "label": self.label,
            "description": self.definition.description,
            "presentation_summary": self.presentation_summary,
            "capability": self.capability.value,
            "availability": self.availability.value,
            "unavailable_reason": self.unavailable_reason,
            "model_route": self.definition.model,
            "tools": list(self.definition.tools),
            "skills": [skill.name for skill in self.verified_skills],
            "permission_mode": self.definition.permission_mode,
            "max_iterations": self.definition.max_iteration_per_run,
            "max_budget_usd": self.definition.max_budget_per_run,
        }


@dataclass(frozen=True, slots=True)
class SpecialistCatalog:
    """Ordered, validated research-specialist catalog."""

    roles: tuple[SpecialistRole, ...]

    @property
    def available_roles(self) -> tuple[SpecialistRole, ...]:
        """Return roles that may be registered with OpenHands."""
        return tuple(
            role for role in self.roles if role.availability == SpecialistAvailability.AVAILABLE
        )

    def role(self, specialist_id: str) -> SpecialistRole:
        """Return one catalog role by its stable identifier."""
        for role in self.roles:
            if role.specialist_id == specialist_id:
                return role
        raise SpecialistCatalogError(f"unknown specialist: {specialist_id}")

    def safe_dict(self) -> dict[str, object]:
        """Return the complete interface-safe catalog."""
        return {"specialists": [role.safe_dict() for role in self.roles]}


def load_specialist_catalog(agents_dir: Path, skills_dir: Path) -> SpecialistCatalog:
    """Load OpenHands definitions and enforce Heartwood's narrower role boundary."""
    resolved_agents_dir = agents_dir.resolve()
    definition_files = (
        tuple(
            path
            for path in sorted(resolved_agents_dir.iterdir())
            if path.is_file()
            and path.suffix.lower() == ".md"
            and path.name not in {"README.md", "readme.md"}
        )
        if resolved_agents_dir.is_dir()
        else ()
    )
    definitions = load_agents_from_dir(resolved_agents_dir)
    if not definitions:
        raise SpecialistCatalogError("the bundled specialist catalog is empty")
    if len(definitions) != len(definition_files):
        raise SpecialistCatalogError("OpenHands could not load every bundled specialist definition")
    available_skills = _load_verified_skills(skills_dir)
    roles = tuple(
        sorted(
            (
                _specialist_role(definition, available_skills=available_skills)
                for definition in definitions
            ),
            key=lambda role: (role.order, role.specialist_id),
        )
    )
    identifiers = [role.specialist_id for role in roles]
    if len(set(identifiers)) != len(identifiers):
        raise SpecialistCatalogError("specialist identifiers must be unique")
    labels = [role.label for role in roles]
    if len(set(labels)) != len(labels):
        raise SpecialistCatalogError("specialist labels must be unique")
    return SpecialistCatalog(roles=roles)


def specialist_agent_factory(
    role: SpecialistRole,
) -> Callable[[LLM], Agent]:
    """Use OpenHands' file-agent factory with Heartwood-verified Skill objects."""
    if role.availability != SpecialistAvailability.AVAILABLE:
        raise SpecialistCatalogError(f"specialist is unavailable: {role.specialist_id}")
    definition_without_skills = role.definition.model_copy(update={"skills": []})
    upstream_factory = agent_definition_to_factory(definition_without_skills)

    def factory(llm: LLM) -> Agent:
        agent = upstream_factory(llm)
        context = agent.agent_context or AgentContext()
        context = context.model_copy(
            update={
                "skills": list(role.verified_skills),
                "load_user_skills": False,
                "load_public_skills": False,
                "load_project_skills": False,
            }
        )
        return agent.model_copy(update={"agent_context": context})

    return factory


def _specialist_role(
    definition: AgentDefinition,
    *,
    available_skills: Mapping[str, Skill],
) -> SpecialistRole:
    metadata = _mapping(definition.metadata.get("heartwood"), definition.name)
    label = _required_string(metadata, "label", definition.name)
    capability = _enum_value(
        SpecialistCapability,
        metadata.get("capability"),
        field="capability",
        specialist_id=definition.name,
    )
    availability = _enum_value(
        SpecialistAvailability,
        metadata.get("availability"),
        field="availability",
        specialist_id=definition.name,
    )
    order = _required_nonnegative_int(metadata, "order", definition.name)
    unavailable_reason = _optional_string(metadata.get("unavailable_reason"))
    if availability == SpecialistAvailability.UNAVAILABLE and unavailable_reason is None:
        raise SpecialistCatalogError(
            f"specialist {definition.name} must explain why it is unavailable"
        )
    if availability == SpecialistAvailability.AVAILABLE and unavailable_reason is not None:
        raise SpecialistCatalogError(
            f"available specialist {definition.name} cannot declare an unavailable reason"
        )
    _validate_openhands_boundary(
        definition,
        availability=availability,
        capability=capability,
    )
    verified_skills: list[Skill] = []
    for skill_name in definition.skills:
        skill = available_skills.get(skill_name)
        if skill is None:
            raise SpecialistCatalogError(
                f"specialist {definition.name} references an unverified Skill: {skill_name}"
            )
        verified_skills.append(skill)
    return SpecialistRole(
        definition=definition,
        label=label,
        capability=capability,
        availability=availability,
        unavailable_reason=unavailable_reason,
        order=order,
        verified_skills=tuple(verified_skills),
    )


def _validate_openhands_boundary(
    definition: AgentDefinition,
    *,
    availability: SpecialistAvailability,
    capability: SpecialistCapability,
) -> None:
    if not definition.description.strip():
        raise SpecialistCatalogError(f"specialist {definition.name} requires a description")
    if not definition.system_prompt.strip():
        raise SpecialistCatalogError(f"specialist {definition.name} requires a system prompt")
    if definition.model != "inherit":
        raise SpecialistCatalogError(
            f"specialist {definition.name} must inherit the parent model route"
        )
    if definition.condenser is not None:
        raise SpecialistCatalogError(
            f"specialist {definition.name} must inherit the default model condenser"
        )
    if definition.permission_mode != "always_confirm":
        raise SpecialistCatalogError(f"specialist {definition.name} must use always_confirm")
    if definition.max_iteration_per_run is None:
        raise SpecialistCatalogError(f"specialist {definition.name} requires an iteration limit")
    if definition.max_budget_per_run is None:
        raise SpecialistCatalogError(f"specialist {definition.name} requires a usage budget")
    if definition.hooks is not None:
        raise SpecialistCatalogError(f"specialist {definition.name} cannot define hooks")
    if definition.mcp_config:
        raise SpecialistCatalogError(f"specialist {definition.name} cannot define MCP servers")
    if definition.profile_store_dir is not None:
        raise SpecialistCatalogError(
            f"specialist {definition.name} cannot select an external model profile store"
        )
    if capability == SpecialistCapability.ADVISORY and definition.tools:
        raise SpecialistCatalogError(f"advisory specialist {definition.name} cannot declare tools")
    if (
        capability == SpecialistCapability.PROJECT_ACTIONS
        and availability == SpecialistAvailability.AVAILABLE
    ):
        # Upstream child-action visibility and restart recovery remain incomplete:
        # https://github.com/OpenHands/software-agent-sdk/issues/4107
        # https://github.com/OpenHands/software-agent-sdk/issues/3907
        raise SpecialistCatalogError(
            f"project-actions specialist {definition.name} must remain unavailable until "
            "child actions are recoverable"
        )
    if capability == SpecialistCapability.PROJECT_ACTIONS and not definition.tools:
        raise SpecialistCatalogError(
            f"project-actions specialist {definition.name} must declare tools"
        )
    if availability == SpecialistAvailability.AVAILABLE and definition.tools:
        raise SpecialistCatalogError(
            f"specialist {definition.name} cannot use tools until child actions are recoverable"
        )
    if availability == SpecialistAvailability.UNAVAILABLE and not definition.tools:
        raise SpecialistCatalogError(
            f"unavailable specialist {definition.name} must declare the blocked tool capability"
        )


def _load_verified_skills(skills_dir: Path) -> dict[str, Skill]:
    repository, knowledge, agent = load_skills_from_dir(skills_dir.resolve())
    combined = {**repository, **knowledge, **agent}
    if len(combined) != len(repository) + len(knowledge) + len(agent):
        raise SpecialistCatalogError("bundled Skill names must be unique")
    return combined


def _mapping(value: object, specialist_id: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecialistCatalogError(
            f"specialist {specialist_id} requires heartwood catalog metadata"
        )
    return cast(Mapping[str, Any], value)


def _required_string(metadata: Mapping[str, Any], field: str, specialist_id: str) -> str:
    value = metadata.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SpecialistCatalogError(f"specialist {specialist_id} requires a non-empty {field}")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SpecialistCatalogError("specialist unavailable_reason must be non-empty")
    return value.strip()


def _required_nonnegative_int(
    metadata: Mapping[str, Any],
    field: str,
    specialist_id: str,
) -> int:
    value = metadata.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SpecialistCatalogError(
            f"specialist {specialist_id} requires a non-negative integer {field}"
        )
    return value


def _enum_value[EnumT: StrEnum](
    enum_type: type[EnumT],
    value: object,
    *,
    field: str,
    specialist_id: str,
) -> EnumT:
    if not isinstance(value, str):
        supported = ", ".join(item.value for item in enum_type)
        raise SpecialistCatalogError(
            f"specialist {specialist_id} {field} must be one of: {supported}"
        )
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        supported = ", ".join(item.value for item in enum_type)
        raise SpecialistCatalogError(
            f"specialist {specialist_id} {field} must be one of: {supported}"
        ) from error
