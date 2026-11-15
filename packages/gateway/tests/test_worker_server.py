# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

import pytest

import heartwood.gateway.worker_server as worker_server


def test_worker_server_selects_specialist_skills_for_the_gateway_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[tuple[Path, Path, str | None]] = []

    def load(agents: Path, skills: Path, *, platform_id: str | None) -> str:
        loaded.append((agents, skills, platform_id))
        return "catalog"

    registered: list[object] = []
    monkeypatch.setattr(worker_server, "load_specialist_catalog", load)
    monkeypatch.setattr(worker_server, "register_specialist_catalog", registered.append)
    monkeypatch.setattr(worker_server, "run_module", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("HEARTWOOD_SKILL_PLATFORM", "terra")

    worker_server.main()

    assert loaded == [
        (
            Path("/opt/heartwood/agents/verified"),
            Path("/opt/heartwood/vendor/heartwood-skills/skills"),
            "terra",
        )
    ]
    assert registered == ["catalog"]
    monkeypatch.delenv("HEARTWOOD_SKILL_PLATFORM")
    with pytest.raises(SystemExit, match="Skill platform"):
        worker_server.main()
