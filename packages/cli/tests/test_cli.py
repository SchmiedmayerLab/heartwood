# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from heartwood.cli import __version__, main
from heartwood.gateway import (
    ActionSettings,
    ActionSettingsStore,
    ModelCatalogError,
    ModelProfile,
    ModelSettings,
    ModelSettingsStore,
    ProviderModel,
    persist_deployment_profile,
)


def test_no_command_prints_help_when_stdin_is_not_interactive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main([])

    assert code == 0
    assert "Auditable agentic coding" in capsys.readouterr().out


def test_version_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--version"])

    assert error.value.code == 0
    assert f"heartwood {__version__}" in capsys.readouterr().out


def test_doctor_reports_setup_without_mutating_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "state" / "sessions"

    assert main(["--workspace", str(workspace), "doctor"]) == 0

    output = capsys.readouterr().out
    assert "State: setup-required" in output
    assert "Setup is incomplete" in output
    assert not (tmp_path / "state").exists()


def test_doctor_supports_machine_readable_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--workspace", str(tmp_path / "state" / "sessions"), "doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "setup-required"
    assert payload["platform_id"] == "generic"


def test_doctor_resolves_workspace_from_heartwood_home(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_HOME", str(tmp_path / "heartwood-home"))

    assert main(["doctor", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    storage = next(item for item in payload["checks"] if item["check_id"] == "state-storage")
    assert str(tmp_path) in storage["summary"]


def test_non_interactive_setup_persists_and_selects_reported_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[tuple[str, str]] = []

    class FakeGateway:
        def __init__(self, *, workspace: Path) -> None:
            self.workspace = workspace

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def select_action_confirmation_mode(self, mode: str) -> dict[str, object]:
            assert mode == "always-confirm"
            return {}

        def discover_models(self, connection_id: str, *, refresh: bool) -> dict[str, object]:
            assert connection_id == "local"
            assert refresh
            return {"models": [{"model_id": "local-model", "availability": "available"}]}

        def connect_model(self, connection_id: str, model_id: str) -> dict[str, object]:
            selected.append((connection_id, model_id))
            return {}

    monkeypatch.setattr("heartwood.cli.SessionGateway", FakeGateway)
    workspace = tmp_path / "state" / "sessions"

    code = main(
        [
            "--workspace",
            str(workspace),
            "setup",
            "--model-source",
            "local",
            "--model-id",
            "local-model",
            "--non-interactive",
            "--yes",
        ]
    )

    assert code == 0
    assert selected == [("local", "local-model")]
    assert (tmp_path / "state" / "setup.json").is_file()
    assert "Setup complete" in capsys.readouterr().out


def test_failed_reconfiguration_restores_the_complete_previous_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "state" / "sessions"
    persist_deployment_profile(workspace, model_source="local", env={})
    ModelSettingsStore(workspace.parent / "models.json").save(
        ModelSettings(
            active_profile="local",
            profiles=(
                ModelProfile(
                    profile_id="local",
                    model="openai/local-model",
                    policy_endpoint="http://127.0.0.1:8765/v1/chat/completions",
                    base_url="http://127.0.0.1:8765/v1",
                    credential_kind="none",
                ),
            ),
        )
    )
    ActionSettingsStore(workspace.parent / "actions.json").save(ActionSettings())
    before = {path.name: path.read_bytes() for path in workspace.parent.iterdir() if path.is_file()}

    class FailingGateway:
        def __init__(self, *, workspace: Path) -> None:
            self.workspace = workspace

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def select_action_confirmation_mode(self, _mode: str) -> dict[str, object]:
            return {}

        def discover_models(self, _connection_id: str, *, refresh: bool) -> dict[str, object]:
            assert refresh
            raise ModelCatalogError("catalog unavailable")

    monkeypatch.setattr("heartwood.cli.SessionGateway", FailingGateway)

    code = main(
        [
            "--workspace",
            str(workspace),
            "setup",
            "--model-source",
            "stanford-ai-api-gateway",
            "--model-id",
            "remote-model",
            "--non-interactive",
            "--yes",
        ]
    )

    after = {path.name: path.read_bytes() for path in workspace.parent.iterdir() if path.is_file()}
    assert code == 1
    assert after == before
    assert "catalog unavailable" in capsys.readouterr().out


def test_carina_setup_refuses_login_node_without_mutating_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "carina")
    workspace = tmp_path / "state" / "sessions"

    code = main(
        [
            "--workspace",
            str(workspace),
            "setup",
            "--model-source",
            "local",
            "--model-id",
            "local-model",
            "--non-interactive",
            "--yes",
        ]
    )

    assert code == 1
    assert "active Slurm compute allocation" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_interactive_setup_can_be_cancelled_without_writing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(["1", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(responses))
    workspace = tmp_path / "state" / "sessions"

    assert main(["--workspace", str(workspace), "setup"]) == 1

    assert "Setup cancelled" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_interactive_setup_handles_closed_input_without_writing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def closed_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed_input)
    workspace = tmp_path / "state" / "sessions"

    assert main(["--workspace", str(workspace), "setup"]) == 1

    assert "Setup cancelled because input closed" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_interactive_setup_handles_closed_confirmation_without_writing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(["1"])

    def closed_confirmation(_prompt: str) -> str:
        try:
            return next(responses)
        except StopIteration as error:
            raise EOFError from error

    monkeypatch.setattr("builtins.input", closed_confirmation)
    workspace = tmp_path / "state" / "sessions"

    assert main(["--workspace", str(workspace), "setup"]) == 1

    assert "Setup cancelled because input closed" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_interactive_setup_rolls_back_when_model_selection_input_closes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGateway:
        def __init__(self, *, workspace: Path) -> None:
            self.workspace = workspace

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def select_action_confirmation_mode(self, _mode: str) -> dict[str, object]:
            return {}

        def discover_models(self, _connection_id: str, *, refresh: bool) -> dict[str, object]:
            assert refresh
            return {"models": [{"model_id": "local-model", "availability": "available"}]}

    def closed_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("heartwood.cli.SessionGateway", FakeGateway)
    monkeypatch.setattr("builtins.input", closed_input)
    workspace = tmp_path / "state" / "sessions"

    assert main(["--workspace", str(workspace), "setup", "--model-source", "local", "--yes"]) == 1

    assert "model selection was cancelled because input closed" in capsys.readouterr().out
    assert not any(path.is_file() for path in (tmp_path / "state").rglob("*"))


def test_non_interactive_setup_requires_explicit_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--workspace", str(tmp_path / "state" / "sessions"), "setup", "--non-interactive"])
    assert error.value.code == 2
    assert "--model-source is required" in capsys.readouterr().err


def test_invalid_session_id_is_reported_as_argument_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--session-id", "../escape", "detect"])

    assert error.value.code == 2
    assert "session id must start with a letter or number" in capsys.readouterr().err


def test_detect_reports_platform_and_dataset(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--workspace", str(tmp_path / "sessions"), "--session-id", "detect", "detect"])

    output = capsys.readouterr().out
    assert code == 0
    assert "Heartwood environment detection" in output
    assert "Platform: generic" in output
    assert "Dataset: omop-cdm" in output


def test_chat_uses_agentic_task_and_shows_pending_action(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_AGENT_BACKEND", "deterministic")
    code = main(
        [
            "--workspace",
            str(tmp_path / "sessions"),
            "--session-id",
            "chat",
            "chat",
            "--prompt",
            "summarize",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "Model route allow" in output
    assert "Agent:" in output
    assert "Action:" in output
    assert "Allow once or reject" in output


def test_allow_once_resumes_pending_action_across_cli_processes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_AGENT_BACKEND", "deterministic")
    workspace = tmp_path / "sessions"
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "--session-id",
                "allow",
                "chat",
                "-p",
                "run",
            ]
        )
        == 0
    )
    capsys.readouterr()

    code = main(
        [
            "--workspace",
            str(workspace),
            "--session-id",
            "allow",
            "allow",
            "allow-toolcall-0",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "Action approved" in output
    assert "Tool heartwood.synthetic.noop exit=0" in output


def test_action_decision_returns_failure_for_unknown_pending_action(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_AGENT_BACKEND", "deterministic")

    code = main(
        [
            "--workspace",
            str(tmp_path / "sessions"),
            "--session-id",
            "missing-action",
            "allow",
            "missing-tool-call",
        ]
    )

    assert code == 1
    assert "no matching pending action" in capsys.readouterr().out


def test_run_remains_a_one_shot_task_alias(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_AGENT_BACKEND", "deterministic")

    code = main(["--workspace", str(tmp_path / "sessions"), "run", "inspect workspace"])

    assert code == 0
    assert "Allow once or reject" in capsys.readouterr().out


def test_actions_selects_low_risk_auto_approval_for_the_shared_runtime(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_AGENT_BACKEND", "deterministic")
    workspace = tmp_path / "sessions"
    base = ["--workspace", str(workspace), "actions"]

    assert main(base) == 0
    assert main([*base, "set", "auto-approve-low-risk"]) == 0
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "--session-id",
                "risk-based",
                "chat",
                "--prompt",
                "summarize",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Ask Every Time" in output
    assert "* Auto-Approve Low Risk" in output
    assert "Tool heartwood.synthetic.noop exit=0" in output
    assert "Allow once or reject" not in output
    assert (
        json.loads((tmp_path / "actions.json").read_text(encoding="utf-8"))["confirmation_mode"]
        == "confirm-risky"
    )


def test_models_add_select_list_and_validate_local_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"
    base = ["--workspace", str(workspace), "models"]

    assert (
        main(
            [
                *base,
                "add",
                "local",
                "--model",
                "openai/local-model",
                "--base-url",
                "http://127.0.0.1:8765/v1",
                "--policy-endpoint",
                "http://127.0.0.1:8765/v1/chat/completions",
                "--credential-kind",
                "none",
                "--select",
            ]
        )
        == 0
    )
    assert main([*base, "list"]) == 0
    assert main([*base, "validate"]) == 0

    output = capsys.readouterr().out
    assert "* local  openai/local-model" in output
    assert "Credentials: configured" in output
    assert "Action confirmation: always-confirm" in output
    assert "Policy: allow" in output
    settings = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert settings["active_profile"] == "local"


def test_models_refresh_and_connect_platform_catalog(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections = tmp_path / "model-connections.json"
    connections.write_text(
        json.dumps(
            {
                "schema_version": "heartwood.model-connections.v1",
                "connections": [
                    {
                        "connection_id": "research-ai",
                        "label": "Research AI Service",
                        "protocol": "static",
                        "model_prefix": "litellm_proxy/",
                        "source": "platform",
                        "credential_kind": "managed-identity",
                        "policy_endpoint": "https://models.example/v1/chat/completions",
                        "catalog_endpoint": None,
                        "description": "Models authorized for this research workspace.",
                        "static_models": ["coding-large", "coding-small"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HEARTWOOD_MODEL_CONNECTIONS", str(connections))
    workspace = tmp_path / "sessions"
    base = ["--workspace", str(workspace), "models"]

    assert main([*base, "refresh", "research-ai"]) == 0
    assert main([*base, "connect", "research-ai", "coding-small"]) == 0

    output = capsys.readouterr().out
    assert "Models available from Research AI Service" in output
    assert "coding-large" in output
    assert "coding-small" in output
    assert "* research-ai  litellm_proxy/coding-small" in output
    settings = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert settings["active_profile"] == "research-ai"
    assert settings["profiles"][0]["model"] == "litellm_proxy/coding-small"


def test_models_use_environment_credential_without_persisting_or_printing_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "runtime-only-secret"
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": "heartwood.policy-profile.v1",
                "policy_id": "provider-test",
                "platform_id": "test",
                "allowed_model_endpoints": ["https://api.openai.com/v1/chat/completions"],
                "allowed_model_catalog_endpoints": ["https://api.openai.com/v1/models"],
                "allowed_capability_tiers": ["supervised", "experimental"],
                "allowed_action_confirmation_modes": ["always-confirm"],
                "credential_allowlist": ["OPENAI_API_KEY"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HEARTWOOD_POLICY_PROFILE", str(policy))
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def list_models(_connection: object, api_key: str | None) -> tuple[ProviderModel, ...]:
        assert api_key == secret
        return (ProviderModel("provider-coder"),)

    monkeypatch.setattr(
        "heartwood.gateway._model_catalog._list_openai_models",
        list_models,
    )
    workspace = tmp_path / "sessions"
    base = ["--workspace", str(workspace), "models"]

    assert main([*base, "refresh", "openai"]) == 0
    assert main([*base, "connect", "openai", "provider-coder"]) == 0

    output = capsys.readouterr().out
    persisted = (tmp_path / "models.json").read_text(encoding="utf-8")
    assert "provider-coder" in output
    assert secret not in output
    assert secret not in persisted
    assert "OPENAI_API_KEY" in persisted


def test_external_model_profile_is_denied_until_policy_allows_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-only-secret")
    workspace = tmp_path / "sessions"
    base = ["--workspace", str(workspace), "models"]
    assert (
        main(
            [
                *base,
                "add",
                "external",
                "--model",
                "openai/configured-model",
                "--policy-endpoint",
                "https://api.openai.com/v1/chat/completions",
                "--api-key-env",
                "OPENAI_API_KEY",
                "--select",
            ]
        )
        == 0
    )

    assert main([*base, "validate"]) == 0

    output = capsys.readouterr().out
    assert "Credentials: available" in output
    assert "Policy: deny" in output
    assert "runtime-only-secret" not in (tmp_path / "models.json").read_text(encoding="utf-8")


def test_models_remove_clears_active_selection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"
    base = ["--workspace", str(workspace), "models"]
    main(
        [
            *base,
            "add",
            "local",
            "--model",
            "openai/local-model",
            "--base-url",
            "http://127.0.0.1:8765/v1",
            "--policy-endpoint",
            "http://127.0.0.1:8765/v1/chat/completions",
            "--credential-kind",
            "none",
            "--select",
        ]
    )
    capsys.readouterr()

    assert main([*base, "remove", "local"]) == 0

    assert "No model profiles configured" in capsys.readouterr().out


def test_skills_list_inspect_install_and_remove_extension(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"
    source = _community_skill(tmp_path)
    base = ["--workspace", str(workspace), "skills"]

    assert main([*base, "list"]) == 0
    assert main([*base, "inspect", str(source)]) == 0
    with pytest.raises(SystemExit) as error:
        main([*base, "install", str(source)])
    assert error.value.code == 2
    assert main([*base, "install", str(source), "--approve"]) == 0
    assert main([*base, "remove", "community-summary"]) == 0

    output = capsys.readouterr()
    assert "aggregate-export  trust=verified  source=bundled" in output.out
    assert "Skill: community-summary" in output.out
    assert "installation approval is required" in output.err
    assert not (tmp_path / "skills" / "community-summary").exists()


def test_interactive_agent_supports_action_and_session_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_AGENT_BACKEND", "deterministic")
    lines = iter(
        [
            "summarize",
            "/allow interactive-toolcall-0",
            "/pause",
            "/resume",
            "/audit-export",
            "/replay",
            "/exit",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(lines))

    code = main(
        [
            "--workspace",
            str(tmp_path / "sessions"),
            "--session-id",
            "interactive",
            "chat",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "Heartwood agent." in output
    assert "You: summarize" in output
    assert "Action approved" in output
    assert "Session paused" in output
    assert "Session resumed" in output
    assert "Audit export:" in output


def test_replay_and_audit_export_use_persisted_events(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"
    output = tmp_path / "audit.jsonl"
    assert main(["--workspace", str(workspace), "--session-id", "audit", "detect"]) == 0
    assert main(["--workspace", str(workspace), "--session-id", "audit", "replay"]) == 0
    assert (
        main(
            [
                "--workspace",
                str(workspace),
                "--session-id",
                "audit",
                "audit",
                "export",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    captured = capsys.readouterr().out
    assert "Detected generic / omop-cdm" in captured
    assert output.is_file()
    assert "audit.export.recorded" in output.read_text(encoding="utf-8")


def test_reviewer_artifacts_remain_available(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "sessions"
    output = tmp_path / "reviewer"
    assert main(["--workspace", str(workspace), "--session-id", "review", "detect"]) == 0

    code = main(
        [
            "--workspace",
            str(workspace),
            "--session-id",
            "review",
            "reviewer",
            "packet",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    assert "Reviewer artifacts:" in capsys.readouterr().out
    assert (output / "reviewer-packet.md").is_file()


def test_serve_requires_built_assets(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="web UI assets not found"):
        main(["serve", "--web-root", str(tmp_path / "missing")])


def _community_skill(tmp_path: Path) -> Path:
    repository_root = Path(__file__).resolve().parents[3]
    source = tmp_path / "source" / "community-summary"
    shutil.copytree(repository_root / "skills" / "verified" / "aggregate-export", source)
    skill_file = source / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8")
        .replace("heartwood.synthetic.aggregate-export", "example.community-summary")
        .replace('name: "aggregate-export"', 'name: "community-summary"')
        .replace('heartwood.trust-tier: "verified"', 'heartwood.trust-tier: "community"'),
        encoding="utf-8",
    )
    metadata_path = source / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["heartwood.trust-tier"] = "community"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return source
