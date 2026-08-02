# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import errno
import hashlib
import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from heartwood.adapters.platform import GenericPlatformAdapter
from heartwood.cli import (
    _MODEL_PREPARATION_ACTIVITY,
    __version__,
    _consume_prompt,
    _float_payload,
    _handle_replay,
    _mapping_payload,
    _run_with_progress,
    _submit_and_wait,
    _submit_with_progress,
    _supports_full_screen_terminal,
    main,
)
from heartwood.cli._interactive import (
    InteractionResult,
    InteractiveSession,
)
from heartwood.gateway import (
    CredentialStore,
    LocalModelChoice,
    LocalModelDownloadPlan,
    ModelArtifact,
    ModelCatalogService,
    ModelConnection,
    ProjectConfig,
    ProjectConfigStore,
    ProjectContext,
    ProjectionLifecycleState,
    ProviderModel,
    RestGateway,
    RestRequest,
    SessionLifecycle,
    SessionProjection,
    SubscriptionDeviceLogin,
    project_session,
)
from heartwood.gateway import (
    SessionGateway as RealSessionGateway,
)
from heartwood.schemas import (
    ModelRepositoryPlanResponse,
    ModelValidationResponse,
    api_response,
)
from heartwood.session import EventKind, SessionEvent


def test_cli_import_keeps_openhands_runtime_lazy() -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            "import sys; import heartwood.cli; assert 'openhands.sdk' not in sys.modules",
        ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_replay_reads_the_committed_stream_without_initializing_the_runtime(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    projection = project_session((), session_id="synthetic-session")

    class ReplayGateway:
        def session_projection(self, *, session_id: str) -> SessionProjection:
            raise AssertionError(f"runtime projection requested for {session_id}")

        def persisted_session_projection(self, *, session_id: str) -> SessionProjection:
            assert session_id == "synthetic-session"
            calls.append("committed")
            return projection

    gateway = cast(RealSessionGateway, ReplayGateway())
    assert _handle_replay(gateway, session_id="synthetic-session") == 0

    captured = capsys.readouterr()
    assert calls == ["committed"]
    assert captured.err == ""


def _run(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> int:
    project.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(project)
    return main(args)


def _install_deterministic_gateway(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_catalog_service: ModelCatalogService | None = None,
    env: dict[str, str] | None = None,
    model_repository: object | None = None,
    subscription_provider: object | None = None,
) -> None:
    def factory(**kwargs: object) -> RealSessionGateway:
        project = kwargs.get("project")
        assert isinstance(project, ProjectContext)
        return RealSessionGateway(
            project=project,
            env={} if env is None else env,
            backend_id="deterministic",
            model_catalog_service=model_catalog_service,
            model_repository=cast(Any, model_repository),
            subscription_provider=cast(Any, subscription_provider),
        )

    monkeypatch.setattr("heartwood.cli.SessionGateway", factory)


def _local_catalog(*, fail: bool = False) -> ModelCatalogService:
    def models(
        _connection: ModelConnection,
        _api_key: str | None,
    ) -> tuple[ProviderModel, ...]:
        if fail:
            raise ConnectionError("synthetic catalog outage")
        return (ProviderModel(model_id="local-model", display_name="Heartwood Model"),)

    return ModelCatalogService(
        openai_lister=models,
        compatibility=lambda _connection, _model: (
            "available",
            "verified",
            32_768,
            True,
        ),
    )


def test_no_command_prints_help_when_stdin_is_not_interactive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    assert "A coding agent for biomedical research projects" in capsys.readouterr().out


def test_keyboard_interrupt_exits_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(_argv: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("heartwood.cli._main", interrupted)

    assert main([]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "\nInterrupted.\n"


def test_version_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--version"])

    assert error.value.code == 0
    assert f"heartwood {__version__}" in capsys.readouterr().out


def test_model_preparation_progress_explains_long_running_work() -> None:
    assert _MODEL_PREPARATION_ACTIVITY.label == "Preparing and verifying the model"
    assert _MODEL_PREPARATION_ACTIVITY.waiting_label == "Still preparing and verifying the model"
    assert _MODEL_PREPARATION_ACTIVITY.guidance == (
        "Large downloads and full verification of existing model files can take several minutes. "
        "Keep this process running."
    )


def test_line_mode_reports_progress_during_slow_model_preparation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def prepare_model() -> str:
        time.sleep(0.03)
        return "ready"

    result = _run_with_progress(
        prepare_model,
        activity=_MODEL_PREPARATION_ACTIVITY,
        update_interval=0.005,
    )

    assert result == "ready"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Preparing and verifying the model..." in captured.err
    assert "Still preparing and verifying the model" in captured.err
    assert "Keep this process running." in captured.err


def test_line_mode_reports_elapsed_progress_for_a_slow_turn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class SlowSession:
        def submit(self, _line: str) -> InteractionResult:
            time.sleep(0.03)
            return InteractionResult(message="complete")

    result = _submit_with_progress(
        cast(InteractiveSession, SlowSession()),
        "inspect the synthetic project",
        update_interval=0.005,
    )

    assert result.message == "complete"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Working on your task" in captured.err
    assert "Still working on your task" in captured.err
    assert "Response time depends on the selected model and task" in captured.err
    assert "managed models may take several minutes" not in captured.err


def test_doctor_is_read_only_and_reports_current_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"

    assert _run(project, monkeypatch, ["doctor"]) == 0

    output = capsys.readouterr().out
    assert f"Project: {project}" in output
    assert f"Heartwood data: {project / '.heartwood'}" in output
    assert "Readiness: setup-required" in output
    assert "Setup is incomplete" in output
    assert not (project / ".heartwood").exists()

    assert _run(project, monkeypatch, ["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project_root"] == str(project)
    assert payload["state_root"] == str(project / ".heartwood")


def test_legacy_path_arguments_and_environment_do_not_change_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    legacy = tmp_path / "legacy"
    monkeypatch.setenv("HEARTWOOD_HOME", str(legacy))
    monkeypatch.setenv("HEARTWOOD_WORKSPACE", str(legacy / "sessions"))

    with pytest.raises(SystemExit) as error:
        _run(project, monkeypatch, ["--workspace", str(legacy), "doctor"])

    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert _run(project, monkeypatch, ["doctor"]) == 0
    assert not (project / ".heartwood").exists()
    assert not legacy.exists()


def test_nested_invocation_directory_is_the_exact_project_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    child = repository / "analysis"
    (repository / ".git").mkdir(parents=True)

    assert _run(child, monkeypatch, ["doctor"]) == 0

    output = capsys.readouterr().out
    assert f"Project: {child}" in output
    assert not (child / ".heartwood").exists()
    assert not (repository / ".heartwood").exists()


def test_non_interactive_setup_persists_one_configuration_and_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    _install_deterministic_gateway(
        monkeypatch,
        model_catalog_service=_local_catalog(),
    )

    code = _run(
        project,
        monkeypatch,
        [
            "setup",
            "--model-source",
            "heartwood",
            "--model-id",
            "local-model",
            "--non-interactive",
            "--yes",
        ],
    )

    config_path = project / ".heartwood" / "config.toml"
    assert code == 0
    assert config_path.is_file()
    assert config_path.stat().st_mode & 0o777 == 0o600
    config = ProjectConfigStore(
        ProjectContext(project),
        ProjectConfig(
            platform_id="generic",
            policy=RealSessionGateway(project=ProjectContext(project), env={})
            .config_store.load()
            .policy,
        ),
    ).load()
    assert config.model_source == "heartwood"
    assert config.model_settings.active_profile == "heartwood"
    assert not any(
        (project / ".heartwood" / name).exists()
        for name in ("setup.json", "policy.json", "models.json", "actions.json")
    )
    assert "Setup complete" in capsys.readouterr().out


def test_non_interactive_local_setup_accepts_one_hugging_face_identifier(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    choice = LocalModelChoice(
        model_id="hf-research-model-123456789abc",
        label="Research Model Q4_K_M",
        purpose="User-selected Hugging Face model.",
        runtime="llama-cpp",
        source_repository="example/research-model-gguf",
        source_revision="1" * 40,
        source_path="model-q4_k_m.gguf",
        size_bytes=7,
        minimum_free_bytes=7,
        license_posture="Source model card reports apache-2.0.",
        catalog_source="user-selected",
        artifact_sha256=hashlib.sha256(b"content").hexdigest(),
        minimum_resource_envelope="Estimated minimum: 4 CPU cores.",
        recommended_resource_envelope="Recommended: 8 CPU cores.",
        recommended_ram_bytes=16 * 1024**3,
        recommended_disk_bytes=21,
    )

    class Repository:
        def plan(self, *_args: object, **_kwargs: object) -> LocalModelDownloadPlan:
            return LocalModelDownloadPlan(choice, "Selected a balanced GGUF model.")

    def download(
        artifact: ModelArtifact,
        *,
        cache_dir: Path,
        progress_callback: object = None,
    ) -> Path:
        del progress_callback
        destination = cache_dir / artifact.artifact_id / artifact.source_path
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"content")
        return destination

    monkeypatch.setattr("heartwood.gateway._gateway.download_artifact", download)
    _install_deterministic_gateway(
        monkeypatch,
        model_catalog_service=_local_catalog(fail=True),
        model_repository=Repository(),
    )

    setup = [
        "setup",
        "--model-source",
        "heartwood",
        "--model-id",
        "example/research-model-gguf",
        "--non-interactive",
        "--yes",
    ]
    assert _run(project, monkeypatch, setup) == 1
    assert "rerun setup with --yes-download" in capsys.readouterr().out
    assert _run(project, monkeypatch, [*setup, "--yes-download"]) == 0

    config = RealSessionGateway(project=ProjectContext(project), env={}).config_store.load()
    assert config.local_model is not None
    assert config.local_model.source_repository == "example/research-model-gguf"
    assert "Heartwood model plan" in capsys.readouterr().out

    class InteractiveInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", InteractiveInput())
    launches: list[object] = []

    def launch(options: object) -> int:
        launches.append(options)
        return 0

    monkeypatch.setattr(
        "heartwood.cli.run_launch",
        launch,
    )
    capsys.readouterr()
    assert _run(project, monkeypatch, []) == 0
    output = capsys.readouterr().out
    assert "The selected Heartwood-managed model is ready to start" in output
    assert launches

    assert _run(project, monkeypatch, ["setup"]) == 0
    output = capsys.readouterr().out
    assert "Setup complete" in output


def test_bare_command_configures_session_token_and_opens_conversation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    observed_tokens: list[str | None] = []

    def models(
        _connection: ModelConnection,
        api_key: str | None,
    ) -> tuple[ProviderModel, ...]:
        observed_tokens.append(api_key)
        return (ProviderModel(model_id="gpt-synthetic", display_name="Synthetic GPT"),)

    service = ModelCatalogService(
        openai_lister=models,
        compatibility=lambda _connection, _model: ("available", "verified", 32_768, True),
    )
    _install_deterministic_gateway(monkeypatch, model_catalog_service=service)

    class InteractiveInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    inputs = iter(["1", "4", "y", "1"])
    monkeypatch.setattr("sys.stdin", InteractiveInput())
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    monkeypatch.setattr("heartwood.cli.getpass.getpass", lambda _prompt: "session-secret")
    opened: list[ModelValidationResponse] = []

    def open_chat(
        gateway: RealSessionGateway,
        *,
        session_id: str,
        plain: bool,
    ) -> int:
        opened.append(gateway.validate_model_profile())
        assert session_id == "session-main"
        assert plain is False
        return 0

    monkeypatch.setattr("heartwood.cli._interactive_chat", open_chat)

    assert _run(project, monkeypatch, []) == 0

    config = (project / ".heartwood" / "config.toml").read_text(encoding="utf-8")
    assert observed_tokens == ["session-secret"]
    assert opened[0]["credential_status"] == "available"
    policy_decision = opened[0]["policy_decision"]
    assert isinstance(policy_decision, dict)
    assert policy_decision["decision"] == "allow"
    assert 'model_source = "openai"' in config
    assert "session-secret" not in config
    output = capsys.readouterr().out
    assert "Setup complete" in output
    assert "session-secret" not in output


def test_bare_command_signs_in_with_chatgpt_through_openhands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SubscriptionProvider:
        connection_id = "openai-subscription"
        vendor = "openai"

        def __init__(self) -> None:
            self.available = False
            self.login_model: str | None = None

        def models(self) -> tuple[str, ...]:
            return ("gpt-subscription",)

        def credential_available(self) -> bool:
            return self.available

        def login(
            self,
            *,
            model: str,
            force_login: bool,  # noqa: ARG002
            open_browser: bool,
            auth_method: Literal["browser", "device_code"],
        ) -> None:
            assert not open_browser
            assert auth_method == "device_code"
            self.login_model = model
            self.available = True

        def start_device_login(self) -> SubscriptionDeviceLogin:
            raise AssertionError("CLI setup should use OpenHands' interactive login")

        def poll_device_login(self, login_id: str) -> SubscriptionDeviceLogin:
            raise AssertionError(f"unexpected device poll: {login_id}")

        def logout(self) -> bool:
            self.available = False
            return True

    provider = SubscriptionProvider()
    service = ModelCatalogService(
        subscription_lister=lambda _connection, _token: (
            ProviderModel(model_id="gpt-subscription"),
        ),
    )
    _install_deterministic_gateway(
        monkeypatch,
        model_catalog_service=service,
        subscription_provider=provider,
    )

    class InteractiveInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    inputs = iter(["1", "3", "y", "1"])
    monkeypatch.setattr("sys.stdin", InteractiveInput())
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    opened: list[ModelValidationResponse] = []

    def open_chat(
        gateway: RealSessionGateway,
        *,
        session_id: str,  # noqa: ARG001
        plain: bool,  # noqa: ARG001
    ) -> int:
        opened.append(gateway.validate_model_profile())
        return 0

    monkeypatch.setattr("heartwood.cli._interactive_chat", open_chat)

    assert _run(tmp_path / "analysis", monkeypatch, []) == 0
    assert provider.login_model == "gpt-subscription"
    assert opened[0]["credential_status"] == "available"
    assert "Sign in with ChatGPT" in capsys.readouterr().out


def test_setup_does_not_claim_a_process_only_credential_is_durable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    service = ModelCatalogService(
        openai_lister=lambda _connection, _api_key: (ProviderModel("gpt-synthetic"),)
    )
    _install_deterministic_gateway(monkeypatch, model_catalog_service=service)
    monkeypatch.setattr("heartwood.cli.getpass.getpass", lambda _prompt: "session-secret")

    code = _run(
        project,
        monkeypatch,
        [
            "setup",
            "--model-source",
            "openai",
            "--model-id",
            "gpt-synthetic",
            "--yes",
        ],
    )

    assert code == 2
    output = capsys.readouterr().out
    assert "Configuration saved" in output
    assert "provider API key was not stored" in output
    assert "Setup complete" not in output
    assert "session-secret" not in output
    assert 'model_source = "openai"' in project.joinpath(".heartwood/config.toml").read_text()


def test_cli_startup_and_doctor_resolve_a_remembered_keyring_credential(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKeyring:
        priority = 1.0

        def __init__(self) -> None:
            self.values: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.values.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.values[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            self.values.pop((service, username), None)

    project_root = tmp_path / "remembered"
    project_root.mkdir()
    project = ProjectContext(project_root)
    keyring = FakeKeyring()
    catalog = ModelCatalogService(
        openai_lister=lambda _connection, _token: (ProviderModel("gpt-synthetic"),),
        compatibility=lambda _connection, _model: (
            "available",
            "verified",
            32_768,
            True,
        ),
    )

    def credential_store() -> CredentialStore:
        return CredentialStore(
            project_root=project.root,
            capabilities=GenericPlatformAdapter().capabilities(),
            env={},
            keyring_backend=keyring,
        )

    configured = RealSessionGateway(
        project=project,
        env={},
        backend_id="deterministic",
        credential_store=credential_store(),
        model_catalog_service=catalog,
    )
    configured.configure_model_source("openai")
    configured.select_action_confirmation_mode("always-confirm")
    configured.discover_models(
        "openai",
        token="remembered-secret",
        refresh=True,
        remember=True,
    )
    configured.connect_model("openai", "gpt-synthetic")
    configured.stop()

    def factory(**kwargs: object) -> RealSessionGateway:
        gateway_project = kwargs.get("project")
        assert isinstance(gateway_project, ProjectContext)
        return RealSessionGateway(
            project=gateway_project,
            env={},
            backend_id="deterministic",
            credential_store=credential_store(),
            model_catalog_service=catalog,
        )

    monkeypatch.setattr("heartwood.cli.SessionGateway", factory)

    assert _run(project_root, monkeypatch, ["doctor"]) == 0
    assert "Readiness: ready" in capsys.readouterr().out
    assert _run(project_root, monkeypatch, ["--prompt", "inspect this project"]) == 0
    output = capsys.readouterr().out
    assert "Setup complete" not in output
    assert "Review 1 action as one OpenHands action set" in output


def test_tokenless_loopback_custom_setup_survives_restart_and_reconfiguration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    observed_tokens: list[str | None] = []

    def models(
        _connection: ModelConnection,
        api_key: str | None,
    ) -> tuple[ProviderModel, ...]:
        observed_tokens.append(api_key)
        return (ProviderModel("local-coder"),)

    _install_deterministic_gateway(
        monkeypatch,
        model_catalog_service=ModelCatalogService(openai_lister=models),
    )
    monkeypatch.setattr(
        "heartwood.cli.getpass.getpass",
        lambda _prompt: pytest.fail("loopback setup must not request a token"),
    )
    setup = [
        "setup",
        "--model-source",
        "custom",
        "--model-id",
        "local-coder",
        "--base-url",
        "http://127.0.0.1:9000/v1",
        "--yes",
    ]

    assert _run(project, monkeypatch, setup) == 0
    assert _run(project, monkeypatch, ["setup", "--yes"]) == 0

    restarted = RealSessionGateway(project=ProjectContext(project), env={})
    settings = restarted.model_settings()
    source_options = settings["source_options"]
    selected = next(option for option in source_options if option["selected"])
    assert selected["source_id"] == "custom"
    assert observed_tokens == [None, None]
    assert "Setup complete" in capsys.readouterr().out


def test_failed_reconfiguration_restores_previous_toml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    _install_deterministic_gateway(monkeypatch, model_catalog_service=_local_catalog())
    setup = [
        "setup",
        "--model-source",
        "heartwood",
        "--model-id",
        "local-model",
        "--non-interactive",
        "--yes",
    ]
    assert _run(project, monkeypatch, setup) == 0
    config_path = project / ".heartwood" / "config.toml"
    previous = config_path.read_bytes()

    _install_deterministic_gateway(
        monkeypatch,
        model_catalog_service=_local_catalog(fail=True),
    )
    assert _run(project, monkeypatch, setup) == 1

    assert config_path.read_bytes() == previous
    assert "did not prepare a usable Heartwood-managed model" in capsys.readouterr().out


def test_unavailable_local_service_points_to_shared_model_setup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    _install_deterministic_gateway(
        monkeypatch,
        model_catalog_service=_local_catalog(fail=True),
    )

    assert (
        _run(
            project,
            monkeypatch,
            [
                "setup",
                "--model-source",
                "heartwood",
                "--model-id",
                "local-model",
                "--non-interactive",
                "--yes",
            ],
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "did not prepare a usable Heartwood-managed model" in output
    assert "recommended model or Other Hugging Face model" in output
    assert "Then run `heartwood`" in output
    assert not (project / ".heartwood" / "config.toml").exists()


def test_local_setup_keeps_slash_model_ids_on_an_existing_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    service_model = "Qwen/Qwen2.5-Coder-7B-Instruct"
    service = ModelCatalogService(
        openai_lister=lambda _connection, _token: (ProviderModel(service_model),),
        compatibility=lambda _connection, _model: ("available", "verified", 32_768, True),
    )

    class UnexpectedRepository:
        def plan(self, *_args: object, **_kwargs: object) -> LocalModelDownloadPlan:
            raise AssertionError("existing service model must not be inspected as a repository")

    _install_deterministic_gateway(
        monkeypatch,
        model_catalog_service=service,
        model_repository=UnexpectedRepository(),
    )

    assert (
        _run(
            project,
            monkeypatch,
            [
                "setup",
                "--model-source",
                "heartwood",
                "--model-id",
                service_model,
                "--non-interactive",
                "--yes",
            ],
        )
        == 0
    )
    config = RealSessionGateway(
        project=ProjectContext(project), env={}, backend_id="deterministic"
    ).config_store.load()
    assert config.model_source == "heartwood"
    assert config.model_settings.profile().model == f"openai/{service_model}"
    assert _run(project, monkeypatch, ["setup"]) == 0


def test_non_interactive_setup_requires_explicit_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit) as missing_source:
        _run(tmp_path, monkeypatch, ["setup", "--non-interactive"])
    assert missing_source.value.code == 2

    with pytest.raises(SystemExit) as missing_model:
        _run(
            tmp_path,
            monkeypatch,
            ["setup", "--model-source", "heartwood", "--non-interactive", "--yes"],
        )
    assert missing_model.value.code == 2

    with pytest.raises(SystemExit) as superseded_source:
        _run(
            tmp_path,
            monkeypatch,
            ["setup", "--model-source", "local", "--model-id", "example"],
        )
    assert superseded_source.value.code == 2


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        (("0",), "no valid model source"),
        (("1", "n"), "Setup cancelled"),
    ],
)
def test_interactive_setup_cancellation_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    responses: tuple[str, ...],
    message: str,
) -> None:
    answers = iter(responses)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    project = tmp_path / "analysis"

    assert _run(project, monkeypatch, ["setup"]) == 1

    assert message in capsys.readouterr().out
    assert not (project / ".heartwood").exists()


def test_interactive_setup_handles_closed_source_and_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def closed(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    source_project = tmp_path / "source-closed"
    assert _run(source_project, monkeypatch, ["setup"]) == 1
    assert "input closed" in capsys.readouterr().out
    assert not (source_project / ".heartwood").exists()

    answers = iter(("1",))

    def closes_after_source(_prompt: str) -> str:
        try:
            return next(answers)
        except StopIteration as error:
            raise EOFError from error

    monkeypatch.setattr("builtins.input", closes_after_source)
    confirmation_project = tmp_path / "confirmation-closed"
    assert _run(confirmation_project, monkeypatch, ["setup"]) == 1
    assert "input closed" in capsys.readouterr().out
    assert not (confirmation_project / ".heartwood").exists()


def test_setup_rolls_back_when_model_or_credential_input_closes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch, model_catalog_service=_local_catalog())

    def closed(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    model_project = tmp_path / "model-closed"
    assert (
        _run(
            model_project,
            monkeypatch,
            ["setup", "--model-source", "heartwood", "--yes"],
        )
        == 1
    )
    assert "model selection was cancelled" in capsys.readouterr().out
    assert not (model_project / ".heartwood" / "config.toml").exists()

    service = ModelCatalogService(
        openai_lister=lambda _connection, _token: (ProviderModel("gpt-synthetic"),),
        compatibility=lambda _connection, _model: ("available", "verified", 32_768, True),
    )
    _install_deterministic_gateway(monkeypatch, model_catalog_service=service)
    monkeypatch.setattr(
        "heartwood.cli.getpass.getpass",
        lambda _prompt: (_ for _ in ()).throw(EOFError),
    )
    token_project = tmp_path / "token-closed"
    assert (
        _run(
            token_project,
            monkeypatch,
            [
                "setup",
                "--model-source",
                "openai",
                "--model-id",
                "gpt-synthetic",
                "--yes",
            ],
        )
        == 1
    )
    assert "credential entry was cancelled" in capsys.readouterr().out
    assert not (token_project / ".heartwood" / "config.toml").exists()


def test_carina_local_setup_rejects_an_unknown_model_without_saving_configuration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "carina")
    project = tmp_path / "carina"

    assert (
        _run(
            project,
            monkeypatch,
            [
                "setup",
                "--model-source",
                "heartwood",
                "--model-id",
                "local-model",
                "--non-interactive",
                "--yes",
            ],
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "unknown Heartwood-managed model in non-interactive setup" in output
    assert "recommended model or Other Hugging Face model" in output
    assert not (project / ".heartwood" / "config.toml").exists()


def test_invalid_session_and_launch_resources_are_argument_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit) as invalid_session:
        _run(tmp_path / "session", monkeypatch, ["--session-id", "../escape", "replay"])
    assert invalid_session.value.code == 2
    assert "session id must start" in capsys.readouterr().err

    with pytest.raises(SystemExit) as invalid_resources:
        _run(tmp_path / "launch", monkeypatch, ["runtime", "start", "--gpus", "0"])
    assert invalid_resources.value.code == 2
    assert "--gpus must be positive" in capsys.readouterr().err

    with pytest.raises(SystemExit) as invalid_timeout:
        _run(
            tmp_path / "timeout",
            monkeypatch,
            ["runtime", "start", "--startup-timeout", "0"],
        )
    assert invalid_timeout.value.code == 2
    assert "--startup-timeout and --port must be positive" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["--port", "0"],
        ["--interface", "web", "--plain"],
        ["--interface", "web", "--prompt", "inspect this project"],
    ],
)
def test_unified_entry_point_rejects_incompatible_global_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        _run(tmp_path, monkeypatch, args)

    assert error.value.code == 2


def test_first_browser_launch_opens_guided_setup_without_terminal_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: list[tuple[Path, str, int]] = []

    def serve(
        *,
        project: ProjectContext,
        host: str,
        port: int,
        web_root: Path,
        base_path: str,
        host_loopback_publication: bool,
    ) -> int:
        assert web_root.name == "dist"
        assert base_path == "/"
        assert host_loopback_publication is True
        observed.append((project.root, host, port))
        return 0

    monkeypatch.setattr("heartwood.cli._handle_serve", serve)

    assert (
        _run(
            tmp_path,
            monkeypatch,
            ["--interface", "web", "--host-loopback-publication"],
        )
        == 0
    )
    assert observed == [(tmp_path, "127.0.0.1", 8767)]
    assert "Opening guided setup in the browser" in capsys.readouterr().out


def test_unsupported_browser_interface_reports_the_platform_constraint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "carina")

    assert _run(tmp_path, monkeypatch, ["--interface", "web"]) == 1
    output = capsys.readouterr().out
    assert "Use the terminal interface in this environment" in output


def test_chat_grouped_approval_and_replay_share_project_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    project = tmp_path / "analysis"
    base = ["--session-id", "synthetic"]
    model_args = [
        "models",
        "add",
        "local-test",
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

    assert _run(project, monkeypatch, model_args) == 0
    assert _run(project, monkeypatch, [*base, "--prompt", "create a summary"]) == 0
    assert _run(project, monkeypatch, [*base, "allow"]) == 0
    assert _run(project, monkeypatch, [*base, "replay"]) == 0

    output = capsys.readouterr().out
    assert "Review 1 action as one OpenHands action set" in output
    assert "Action set approved (1 action)" in output
    assert (project / ".heartwood" / "sessions" / "synthetic" / "events.jsonl").is_file()


def test_cli_inspects_bounded_project_files_and_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch)
    project = tmp_path / "analysis"
    project.mkdir()
    (project / "analysis.py").write_text("answer = 42\n", encoding="utf-8")
    private = project / "nested" / ".heartwood"
    private.mkdir(parents=True)
    (private / "private.txt").write_text(
        "not visible",
        encoding="utf-8",
    )

    assert _run(project, monkeypatch, ["files", "list"]) == 0
    assert _run(project, monkeypatch, ["files", "show", "analysis.py"]) == 0
    assert _run(project, monkeypatch, ["changes"]) == 0

    output = capsys.readouterr().out
    assert "Project files · ." in output
    assert "analysis.py" in output
    assert "answer = 42" in output
    assert "Project changes · Session actions" in output
    assert "private.txt" not in output
    assert "\x1b[" not in output


def test_cli_file_commands_report_invalid_paths_and_statuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch)
    project = tmp_path / "analysis"
    project.mkdir()
    (project / "analysis.py").write_text("answer = 42\n", encoding="utf-8")

    assert _run(project, monkeypatch, ["files", "show", "missing.py"]) == 1
    assert _run(project, monkeypatch, ["changes", "analysis.py"]) == 1
    assert _run(project, monkeypatch, ["files", "show", "../outside.txt"]) == 64

    captured = capsys.readouterr()
    assert "File does not exist" in captured.out
    assert "not present in the bounded changed-file list" in captured.out
    assert "Project files unavailable: HW-WORKSPACE-001" in captured.err


def test_cli_file_commands_reject_invalid_subcommands_and_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch)
    project = tmp_path / "analysis"

    with pytest.raises(SystemExit) as missing_subcommand:
        _run(project, monkeypatch, ["files"])
    with pytest.raises(SystemExit) as invalid_depth:
        _run(project, monkeypatch, ["files", "list", "--depth", "0"])

    assert missing_subcommand.value.code == 2
    assert invalid_depth.value.code == 2


def test_internal_prompt_handoff_is_project_private_and_consumed(tmp_path: Path) -> None:
    project = ProjectContext(tmp_path)
    project.initialize()
    prompt_file = project.runtime_dir / "pending-prompt.synthetic.txt"
    prompt_file.write_text("inspect the synthetic project", encoding="utf-8")
    prompt_file.chmod(0o600)

    assert _consume_prompt(project, None, prompt_file) == "inspect the synthetic project"
    assert not prompt_file.exists()

    outside = tmp_path / "outside-prompt.txt"
    outside.write_text("must remain", encoding="utf-8")
    with pytest.raises(ValueError, match="outside this project's private runtime state"):
        _consume_prompt(project, None, outside)
    assert outside.read_text(encoding="utf-8") == "must remain"


def test_one_shot_aliases_and_unknown_action_return_meaningful_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch)
    project = tmp_path / "analysis"

    assert (
        _run(
            project,
            monkeypatch,
            [
                "models",
                "add",
                "local-test",
                "--model",
                "openai/local-model",
                "--base-url",
                "http://127.0.0.1:8765/v1",
                "--policy-endpoint",
                "http://127.0.0.1:8765/v1/chat/completions",
                "--credential-kind",
                "none",
                "--select",
            ],
        )
        == 0
    )
    assert _run(project, monkeypatch, ["--prompt", "inspect the project"]) == 0
    assert _run(project, monkeypatch, ["allow", "missing-action"]) == 1
    assert _run(project, monkeypatch, ["reject"]) == 0
    assert _run(project, monkeypatch, ["pause"]) == 1
    assert _run(project, monkeypatch, ["resume"]) == 1

    output = capsys.readouterr().out
    assert "Review 1 action as one OpenHands action set" in output
    assert "no matching pending action" in output
    assert "Action set rejected" in output
    assert "pause is unavailable while the agent is idle" in output
    assert "resume is unavailable while the agent is idle" in output


def test_action_alias_reports_gateway_error_event(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch)
    error_event = SessionEvent(
        event_id="decision-error",
        session_id="gateway-error",
        sequence=0,
        kind=EventKind.ERROR_RECORDED,
        occurred_at="2026-07-13T00:00:00Z",
        payload={"reason": "synthetic gateway failure"},
    )
    monkeypatch.setattr(
        InteractiveSession,
        "submit",
        lambda _session, _directive: InteractionResult(
            events=(error_event,),
            projection=project_session((error_event,), session_id="gateway-error"),
        ),
    )

    assert _run(tmp_path, monkeypatch, ["--session-id", "gateway-error", "approve"]) == 1
    assert "synthetic gateway failure" in capsys.readouterr().out


def test_one_shot_interaction_waits_for_background_work() -> None:
    class BackgroundSession:
        waited = False

        def submit(self, line: str) -> InteractionResult:
            assert line == "/allow"
            return InteractionResult(
                projection=SessionProjection(
                    session_id="background",
                    event_count=1,
                    revision=0,
                    lifecycle=ProjectionLifecycleState(
                        status=SessionLifecycle.RUNNING,
                        can_pause=True,
                    ),
                )
            )

        def wait_until_stable(self) -> SessionProjection:
            self.waited = True
            return SessionProjection(
                session_id="background",
                event_count=2,
                revision=1,
                lifecycle=ProjectionLifecycleState(status=SessionLifecycle.FINISHED),
            )

    session = BackgroundSession()
    result = _submit_and_wait(cast(InteractiveSession, session), "/allow")

    assert session.waited
    assert result.projection is not None
    assert result.projection.lifecycle.status == "finished"


def test_interactive_chat_does_not_repeat_live_user_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch)
    assert (
        _run(
            tmp_path,
            monkeypatch,
            [
                "models",
                "add",
                "local-test",
                "--model",
                "openai/local-model",
                "--base-url",
                "http://127.0.0.1:8765/v1",
                "--policy-endpoint",
                "http://127.0.0.1:8765/v1/chat/completions",
                "--credential-kind",
                "none",
                "--select",
            ],
        )
        == 0
    )
    lines = iter(["summarize", "/reject", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(lines))

    class InteractiveInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", InteractiveInput())

    assert _run(tmp_path, monkeypatch, ["--session-id", "interactive", "--plain"]) == 0

    output = capsys.readouterr().out
    assert "Heartwood agent." in output
    assert "You: summarize" not in output
    assert "Review 1 action as one OpenHands action set" in output
    assert "Action set rejected (1 action)" in output


def test_actions_and_advanced_model_profile_persist_in_config_toml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "heartwood.gateway._credentials._system_keyring_backend",
        lambda: None,
    )
    project = tmp_path / "analysis"
    model_args = [
        "models",
        "add",
        "local-test",
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

    assert _run(project, monkeypatch, model_args) == 0
    assert _run(project, monkeypatch, ["actions", "set", "auto-approve-low-risk"]) == 0
    assert _run(project, monkeypatch, ["models", "validate", "local-test"]) == 0

    contents = (project / ".heartwood" / "config.toml").read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert 'active_profile = "local-test"' in contents
    assert 'confirmation_mode = "confirm-risky"' in contents
    assert "Profile: local-test" in output
    assert "Low-Risk Automation" in output


def test_cli_and_browser_gateway_observe_the_same_project_configuration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    catalog = _local_catalog()

    assert _run(project, monkeypatch, ["actions", "set", "auto-approve-low-risk"]) == 0
    capsys.readouterr()

    gateway = RealSessionGateway(
        project=ProjectContext(project),
        env={},
        backend_id="deterministic",
        model_catalog_service=catalog,
    )
    browser = RestGateway(gateway)
    action_settings = browser.handle(RestRequest(method="GET", path="/settings/actions"))
    assert action_settings.body["confirmation_mode"] == "confirm-risky"
    assert (
        browser.handle(
            RestRequest(
                method="PUT",
                path="/settings/actions/confirmation",
                body=json.dumps({"mode": "always-confirm"}),
            )
        ).status_code
        == 200
    )
    assert (
        browser.handle(
            RestRequest(
                method="POST",
                path="/settings/models/catalog",
                body=json.dumps({"connection_id": "heartwood", "refresh": True}),
            )
        ).status_code
        == 200
    )
    assert (
        browser.handle(
            RestRequest(
                method="POST",
                path="/settings/models/connect",
                body=json.dumps({"connection_id": "heartwood", "model_id": "local-model"}),
            )
        ).status_code
        == 200
    )

    _install_deterministic_gateway(monkeypatch, model_catalog_service=catalog)
    assert _run(project, monkeypatch, ["actions"]) == 0
    assert _run(project, monkeypatch, ["models", "list"]) == 0
    output = capsys.readouterr().out
    assert "* Review Every Action" in output
    assert "* heartwood  openai/local-model" in output


def test_models_list_select_remove_and_artifacts_use_one_configuration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    first = [
        "models",
        "add",
        "first",
        "--model",
        "openai/first",
        "--base-url",
        "http://127.0.0.1:8765/v1",
        "--policy-endpoint",
        "http://127.0.0.1:8765/v1/chat/completions",
        "--credential-kind",
        "none",
    ]
    second = [
        "models",
        "add",
        "second",
        "--model",
        "openai/second",
        "--base-url",
        "http://127.0.0.1:8765/v1",
        "--policy-endpoint",
        "http://127.0.0.1:8765/v1/chat/completions",
        "--credential-kind",
        "none",
    ]

    assert _run(project, monkeypatch, first) == 0
    assert _run(project, monkeypatch, second) == 0
    assert _run(project, monkeypatch, ["models", "select", "second"]) == 0
    assert _run(project, monkeypatch, ["models", "list"]) == 0
    assert _run(project, monkeypatch, ["models", "managed"]) == 0
    assert _run(project, monkeypatch, ["models", "remove", "second"]) == 0

    output = capsys.readouterr().out
    assert "* second" in output
    assert "Models Heartwood can run" in output
    assert "No model profiles configured" not in output


def test_models_connect_prepares_stanford_source_in_a_fresh_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    observed: list[tuple[str, str | None]] = []

    def models(
        connection: ModelConnection,
        api_key: str | None,
    ) -> tuple[ProviderModel, ...]:
        observed.append((connection.connection_id, api_key))
        return (ProviderModel(model_id="gpt-synthetic"),)

    _install_deterministic_gateway(
        monkeypatch,
        env={"STANFORD_AI_API_KEY": "external-secret"},
        model_catalog_service=ModelCatalogService(
            openai_lister=models,
            compatibility=lambda _connection, _model: (
                "available",
                "verified",
                32_768,
                True,
            ),
        ),
    )

    assert (
        _run(
            project,
            monkeypatch,
            ["models", "connect", "stanford-ai-api-gateway", "gpt-synthetic"],
        )
        == 0
    )

    assert observed == [("stanford-ai-api-gateway", "external-secret")]
    output = capsys.readouterr().out
    assert "* stanford-ai-api-gateway  openai/gpt-synthetic" in output
    assert "Policy: allow" in output
    config = (project / ".heartwood" / "config.toml").read_text(encoding="utf-8")
    assert 'model_source = "stanford-ai-api-gateway"' in config
    assert "external-secret" not in config


def test_cli_imports_a_local_model_and_forgets_provider_credentials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    source = tmp_path / "model.gguf"
    source.write_bytes(b"GGUFsynthetic-model")
    _install_deterministic_gateway(monkeypatch)

    assert (
        _run(
            project,
            monkeypatch,
            [
                "models",
                "import",
                str(source),
                "--source",
                "example/research-model",
                "--revision",
                "1" * 40,
                "--license",
                "Apache-2.0",
            ],
        )
        == 0
    )
    assert _run(project, monkeypatch, ["models", "forget", "openai"]) == 0

    captured = capsys.readouterr()
    assert "research-model is ready in this project" in captured.out
    assert "Forgot the saved credential for openai" in captured.out
    assert "Preparing and verifying the model" in captured.err
    assert (project / ".heartwood" / "models").is_dir()


def test_cli_plans_and_downloads_hugging_face_identifier_without_runtime_flags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    calls: list[tuple[str, str | None]] = []
    choice = LocalModelChoice(
        model_id="hf-research-model-123456789abc",
        label="Research Model Q4_K_M",
        purpose="User-selected Hugging Face model.",
        runtime="llama-cpp",
        source_repository="example/research-model-gguf",
        source_revision="1" * 40,
        source_path="model-q4_k_m.gguf",
        size_bytes=4 * 1024**3,
        minimum_free_bytes=5 * 1024**3,
        license_posture="Source model card reports apache-2.0.",
        catalog_source="user-selected",
        artifact_sha256="a" * 64,
        minimum_resource_envelope="Estimated minimum: 4 CPU cores.",
        recommended_resource_envelope="Recommended: 8 CPU cores.",
        recommended_ram_bytes=16 * 1024**3,
        recommended_disk_bytes=6 * 1024**3,
    )
    plan = api_response(
        ModelRepositoryPlanResponse,
        {
            "model": {
                **choice.safe_dict(),
                "active": False,
                "available": True,
                "selected": False,
                "availability_reason": "Available on this deployment",
                "recommended": False,
            },
            "selection_reason": "Selected a balanced single-file GGUF variant.",
        },
    )

    def inspect(
        _gateway: RealSessionGateway,
        repository: str,
        *,
        revision: str | None = None,
    ) -> ModelRepositoryPlanResponse:
        calls.append((f"inspect:{repository}", revision))
        return plan

    def download(
        gateway: RealSessionGateway,
        repository: str,
        *,
        revision: str | None = None,
    ) -> Path:
        calls.append((f"download:{repository}", revision))
        return gateway.project.models_dir / "hf-research-model" / "model.gguf"

    monkeypatch.setattr(RealSessionGateway, "inspect_model_repository", inspect)
    monkeypatch.setattr(RealSessionGateway, "download_custom_local_model_now", download)
    _install_deterministic_gateway(monkeypatch, model_catalog_service=_local_catalog(fail=True))

    assert _run(project, monkeypatch, ["models", "inspect", "example/research-model-gguf"]) == 0
    assert _run(project, monkeypatch, ["models", "download", "example/research-model-gguf"]) == 0

    captured = capsys.readouterr()
    output = captured.out
    assert "Heartwood model plan" in output
    assert "Runtime: CPU" in output
    assert "Recommended: 8 CPU cores" in output
    assert "Preparing and verifying the model" in captured.err
    assert "Model files are ready:" in output
    assert "Run `heartwood` to continue setup or open Heartwood." in output
    assert calls == [
        ("inspect:example/research-model-gguf", None),
        ("download:example/research-model-gguf", None),
    ]


def test_model_catalog_refresh_and_connect_use_shared_gateway_service(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch, model_catalog_service=_local_catalog())

    assert _run(tmp_path, monkeypatch, ["models", "refresh", "heartwood"]) == 0
    assert _run(tmp_path, monkeypatch, ["models", "connect", "heartwood", "local-model"]) == 0

    output = capsys.readouterr().out
    assert "Heartwood Model (local-model)" in output
    assert "Active and saved profiles" in output
    assert "Credential isolation:" in output
    assert "Profile: heartwood" in output
    assert "selected Heartwood-managed model" in output
    assert "model profile local" not in output


def test_model_catalog_rejects_superseded_connection_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_gateway(monkeypatch, model_catalog_service=_local_catalog())

    with pytest.raises(SystemExit):
        _run(tmp_path, monkeypatch, ["models", "refresh", "local"])

    assert "unknown model connection: local" in capsys.readouterr().err


def test_skills_inspect_install_and_remove_use_project_local_extensions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    source = _community_skill(tmp_path)

    assert _run(project, monkeypatch, ["skills", "list"]) == 0
    assert _run(project, monkeypatch, ["skills", "inspect", str(source)]) == 0
    with pytest.raises(SystemExit) as approval:
        _run(project, monkeypatch, ["skills", "install", str(source)])
    assert approval.value.code == 2
    assert _run(project, monkeypatch, ["skills", "install", str(source), "--approve"]) == 0
    assert (project / ".heartwood" / "skills" / "community-summary").is_dir()
    assert _run(project, monkeypatch, ["skills", "remove", "community-summary"]) == 0
    assert not (project / ".heartwood" / "skills" / "community-summary").exists()

    captured = capsys.readouterr()
    assert "aggregate-export  trust=verified  source=bundled" in captured.out
    assert "Skill: community-summary" in captured.out
    assert "installation approval is required" in captured.err


def test_audit_export_uses_project_sessions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    audit = tmp_path / "audit.jsonl"
    _install_deterministic_gateway(monkeypatch)
    project.mkdir()
    ProjectContext(project).initialize()

    assert (
        _run(
            project,
            monkeypatch,
            ["--session-id", "review", "audit", "export", "--output", str(audit)],
        )
        == 0
    )
    assert "audit.export.recorded" in audit.read_text(encoding="utf-8")
    assert "Audit export" in capsys.readouterr().out


def test_audit_verify_checkpoint_and_verification_are_operator_workflows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    deployment = tmp_path / "deployment"
    private_key, public_key = _audit_key_pair(deployment)
    bundle = deployment / "review-checkpoint"
    _install_deterministic_gateway(monkeypatch)

    assert (
        _run(
            project,
            monkeypatch,
            [
                "--session-id",
                "review",
                "audit",
                "checkpoint",
                "--output",
                str(bundle),
                "--deployment-id",
                "generic-research",
                "--retention-policy",
                "research-audit-7y",
                "--retain-until",
                "2033-08-02",
                "--signing-key",
                str(private_key),
            ],
        )
        == 0
    )
    assert _run(project, monkeypatch, ["--session-id", "review", "audit", "verify"]) == 0
    assert (
        _run(
            project,
            monkeypatch,
            [
                "audit",
                "verify-checkpoint",
                str(bundle),
                "--public-key",
                str(public_key),
            ],
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Audit checkpoint:" in output
    assert "Audit history verified" in output
    assert "Audit checkpoint verified" in output
    assert "generic-research" in output
    assert "research-audit-7y through 2033-08-02" in output


def test_audit_checkpoint_reports_project_boundary_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    private_key, _public_key = _audit_key_pair(tmp_path / "deployment")
    _install_deterministic_gateway(monkeypatch)

    result = _run(
        project,
        monkeypatch,
        [
            "audit",
            "checkpoint",
            "--output",
            str(project / "checkpoint"),
            "--deployment-id",
            "generic-research",
            "--retention-policy",
            "research-audit-7y",
            "--retain-until",
            "2033-08-02",
            "--signing-key",
            str(private_key),
        ],
    )

    assert result == 64
    captured = capsys.readouterr()
    assert "must be outside the Heartwood project" in captured.err
    assert "Traceback" not in captured.err


def test_cli_reports_active_browser_session_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "analysis"
    project.mkdir()
    _install_deterministic_gateway(monkeypatch)
    browser_gateway = RealSessionGateway(
        project=ProjectContext(project),
        env={},
        backend_id="deterministic",
    )
    browser = RestGateway(browser_gateway)
    command = json.dumps(
        {
            "schema_version": "heartwood.session-command.v1",
            "command_id": "browser-pause",
            "session_id": "review",
            "kind": "pause",
            "actor_id": "browser",
            "created_at": "2026-01-01T00:00:00Z",
            "payload": {},
        }
    )
    assert (
        browser.handle(
            RestRequest(
                method="POST",
                path="/sessions/review/commands",
                body=command,
            )
        ).status_code
        == 200
    )

    assert _run(project, monkeypatch, ["--session-id", "review", "resume"]) == 75

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Session unavailable: session review is active in another Heartwood process" in (
        captured.err
    )
    browser_gateway.stop()


def test_cli_reports_unsupported_session_storage_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def native_lock_unavailable(_descriptor: int) -> bool:
        raise OSError(errno.ENOSYS, "synthetic unsupported filesystem")

    project = tmp_path / "analysis"
    project.mkdir()
    _install_deterministic_gateway(monkeypatch)
    monkeypatch.setattr("filelock._unix._lock_fd_nonblocking", native_lock_unavailable)

    assert _run(project, monkeypatch, ["--session-id", "review", "pause"]) == 75

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Session unavailable: storage does not support the required native lock" in captured.err


def test_serve_requires_built_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="web UI assets not found"):
        _run(
            tmp_path,
            monkeypatch,
            ["gateway", "serve", "--web-root", str(tmp_path / "missing")],
        )


def test_serve_starts_gateway_for_current_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "analysis"
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("<main>Heartwood</main>\n", encoding="utf-8")
    observed: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.run",
        lambda _app, *, host, port, log_level, proxy_headers: observed.append(  # noqa: ARG005, F841, RUF100
            (host, port)
        ),
    )
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "terra")
    monkeypatch.setenv("GOOGLE_PROJECT", "terra-project")
    monkeypatch.setenv("CLUSTER_NAME", "saturn-runtime")

    assert (
        _run(
            project,
            monkeypatch,
            [
                "gateway",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "9876",
                "--web-root",
                str(web_root),
                "--ingress-mode",
                "jupyter-proxy",
                "--public-origin",
                "https://notebooks.firecloud.org",
                "--base-path",
                "/proxy/9876/",
            ],
        )
        == 0
    )
    assert observed == [("127.0.0.1", 9876)]
    assert not (project / ".heartwood").exists()
    assert capsys.readouterr().out == ""


def test_serve_rejects_non_loopback_and_incomplete_proxy_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("<main>Heartwood</main>\n", encoding="utf-8")
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.run",
        lambda *_args, **_kwargs: pytest.fail("unsafe gateway was started"),
    )
    monkeypatch.setenv("HEARTWOOD_IMAGE_FLAVOR", "standard")

    with pytest.raises(SystemExit, match=r"HW-INGRESS-001.*loopback bind"):
        _run(
            tmp_path,
            monkeypatch,
            [
                "gateway",
                "serve",
                "--host",
                "0.0.0.0",
                "--web-root",
                str(web_root),
            ],
        )
    with pytest.raises(SystemExit, match=r"HW-INGRESS-001.*proxy source"):
        _run(
            tmp_path,
            monkeypatch,
            [
                "gateway",
                "serve",
                "--host",
                "0.0.0.0",
                "--web-root",
                str(web_root),
                "--ingress-mode",
                "trusted-proxy",
                "--public-origin",
                "https://heartwood.example",
            ],
        )


def test_serve_requires_an_explicit_host_loopback_publication_assertion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("<main>Heartwood</main>\n", encoding="utf-8")
    observed: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.run",
        lambda _app, *, host, port, log_level, proxy_headers: observed.append(  # noqa: ARG005, F841, RUF100
            (host, port)
        ),
    )

    assert (
        _run(
            tmp_path,
            monkeypatch,
            [
                "gateway",
                "serve",
                "--host",
                "0.0.0.0",
                "--web-root",
                str(web_root),
                "--host-loopback-publication",
            ],
        )
        == 0
    )
    assert observed == [("0.0.0.0", 8767)]


def test_serve_uses_the_detected_platform_ingress_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_root = tmp_path / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("<main>Heartwood</main>\n", encoding="utf-8")
    monkeypatch.setenv("HEARTWOOD_PLATFORM", "terra")
    monkeypatch.setenv("GOOGLE_PROJECT", "terra-project")
    monkeypatch.setenv("CLUSTER_NAME", "saturn-runtime")
    monkeypatch.setattr(
        "heartwood.cli.uvicorn.run",
        lambda *_args, **_kwargs: pytest.fail("incomplete gateway route was started"),
    )

    with pytest.raises(SystemExit, match=r"HW-INGRESS-001.*exact external origin"):
        _run(
            tmp_path,
            monkeypatch,
            [
                "gateway",
                "serve",
                "--web-root",
                str(web_root),
                "--base-path",
                "/proxy/8767",
            ],
        )


def test_cli_helpers_fail_closed_on_malformed_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TypeError, match="expected dataset payload"):
        _mapping_payload([], "dataset")
    with pytest.raises(TypeError, match="expected a numeric payload"):
        _float_payload(True)
    assert _float_payload(1) == 1.0

    monkeypatch.setattr("heartwood.cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("heartwood.cli.sys.stdout.isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert _supports_full_screen_terminal()
    monkeypatch.setenv("TERM", "dumb")
    assert not _supports_full_screen_terminal()


def _audit_key_pair(root: Path) -> tuple[Path, Path]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private_path = root / "private.pem"
    public_path = root / "public.pem"
    private_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    public_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


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
