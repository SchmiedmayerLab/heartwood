# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from heartwood_platform.control.registry import NotAMemberError, Project, Registry

_DEMO = Project("demo", "grant-demo", "policy-1", frozenset({"alice", "bob"}))
_OTHER = Project("other", "grant-other", "policy-1", frozenset({"alice"}))


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    registry = Registry(tmp_path / "control.sqlite3")
    registry.synchronize([_DEMO, _OTHER])
    return registry


def test_members_see_only_their_projects(registry: Registry) -> None:
    assert [project.id for project in registry.projects_of("alice")] == ["demo", "other"]
    assert [project.id for project in registry.projects_of("bob")] == ["demo"]
    assert registry.projects_of("mallory") == []
    assert registry.project("bob", "demo").billing_attribution_ref == "grant-demo"


@pytest.mark.parametrize(("subject", "project"), [("bob", "other"), ("alice", "missing")])
def test_other_projects_are_refused_alike(registry: Registry, subject: str, project: str) -> None:
    with pytest.raises(NotAMemberError):
        registry.project(subject, project)


def test_synchronization_replaces_projects_and_members_and_keeps_sessions(
    registry: Registry,
) -> None:
    session = registry.session("bob", "demo", create=True)

    registry.synchronize([Project("demo", "grant-new", "policy-2", frozenset({"alice"}))])

    assert registry.projects_of("bob") == []
    assert [project.id for project in registry.projects_of("alice")] == ["demo"]
    assert registry.project("alice", "demo").billing_attribution_ref == "grant-new"
    assert registry.session("bob", "demo") == session


def test_session_is_created_once_and_survives_a_restart(registry: Registry, tmp_path: Path) -> None:
    assert registry.session("alice", "demo") is None

    session = registry.session("alice", "demo", create=True)

    assert session is not None
    assert len(session.id) == 16
    assert registry.session("alice", "demo", create=True) == session
    assert registry.session("alice", "other", create=True) != session
    assert Registry(tmp_path / "control.sqlite3").session("alice", "demo") == session

    registry.delete_session("alice", "demo")
    assert registry.session("alice", "demo") is None


def test_concurrent_launches_share_one_session(registry: Registry) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        sessions = list(
            pool.map(lambda _: registry.session("alice", "demo", create=True), range(32))
        )

    assert len({session.id for session in sessions if session is not None}) == 1
