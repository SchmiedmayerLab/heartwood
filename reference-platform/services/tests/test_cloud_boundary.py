# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPOSITORY = Path(__file__).resolve().parents[3]


def _boundary() -> ModuleType:
    path = _REPOSITORY / "reference-platform" / "tools" / "check_cloud_boundary.py"
    spec = importlib.util.spec_from_file_location("check_cloud_boundary", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


boundary = _boundary()


def _package(root: Path, name: str = "sample", dependencies: str = "") -> Path:
    package = root / "packages" / name
    source = package / "src" / "heartwood" / name
    source.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        f'[project]\nname = "heartwood-{name}"\ndependencies = [{dependencies}]\n',
        encoding="utf-8",
    )
    (source / "__init__.py").write_text('"""Sample package."""\n', encoding="utf-8")
    return source


def _services(root: Path, dependencies: str, source: str) -> None:
    services = root / "reference-platform" / "services"
    module = services / "src" / "heartwood_platform"
    module.mkdir(parents=True)
    (services / "pyproject.toml").write_text(
        f'[project]\nname = "heartwood-reference-platform"\ndependencies = [{dependencies}]\n',
        encoding="utf-8",
    )
    (module / "__init__.py").write_text(source, encoding="utf-8")


def test_repository_passes_with_every_allowance_in_use() -> None:
    assert boundary.check(_REPOSITORY, boundary.ALLOWLIST) == []
    assert boundary.main(["--root", str(_REPOSITORY)]) == 0


def test_clean_package_passes(tmp_path: Path) -> None:
    source = _package(tmp_path, dependencies='"httpx==0.28.1"')
    (source / "client.py").write_text("import httpx\n\nURL = 'https://example.org'\n")

    assert boundary.check(tmp_path, ()) == []


def test_planted_cloud_import_and_identifier_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _package(tmp_path)
    (source / "storage.py").write_text(
        "from google.cloud import storage\n\nBUCKET = 'https://storage.googleapis.com/data'\n"
    )

    errors = boundary.check(tmp_path, ())

    assert errors == [
        "packages/sample/src/heartwood/sample/storage.py:1: imports cloud SDK google.cloud",
        "packages/sample/src/heartwood/sample/storage.py:3: "
        "names cloud infrastructure storage.googleapis.com",
    ]
    assert boundary.main(["--root", str(tmp_path)]) == 1
    assert "storage.googleapis.com" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("statement", "module"),
    [
        ("import boto3", "boto3"),
        ("import azure.identity as identity", "azure.identity"),
        ("from google import auth", "google.auth"),
        ("import importlib\nimportlib.import_module('botocore.session')", "botocore.session"),
        ("__import__('googleapiclient.discovery')", "googleapiclient.discovery"),
    ],
)
def test_static_and_literal_dynamic_imports_are_found(
    tmp_path: Path, statement: str, module: str
) -> None:
    source = _package(tmp_path)
    (source / "client.py").write_text(f"{statement}\n")

    errors = boundary.check(tmp_path, ())

    assert any(error.endswith(f"imports cloud SDK {module}") for error in errors)


@pytest.mark.parametrize(
    "identifier",
    [
        "s3.us-east-1.amazonaws.com",
        "account.blob.core.windows.net",
        "management.azure.com",
        "metadata.google.internal",
        "169.254.169.254",
        "fd00:ec2::254",
        "cloud.google.com/gke-nodepool",
        "iam.gke.io/gcp-service-account",
        "pd.csi.storage.gke.io",
        "ebs.csi.aws.com",
        "standard-rwo",
        "pd-ssd",
        "hyperdisk-balanced",
        "googleapis.com",
        "vault.azure.net",
        "registry.azurecr.io",
    ],
)
def test_cloud_identifiers_are_found_in_any_source_file(tmp_path: Path, identifier: str) -> None:
    source = _package(tmp_path)
    (source / "settings.json").write_text(json.dumps({"value": identifier}))

    assert boundary.check(tmp_path, ()) == [
        f"packages/sample/src/heartwood/sample/settings.json:1: "
        f"names cloud infrastructure {identifier}"
    ]


def test_provider_names_in_model_routing_are_outside_the_check(tmp_path: Path) -> None:
    source = _package(tmp_path)
    (source / "routes.py").write_text("PREFIXES = ('azure/', 'bedrock/', 'vertex_ai/')\n")

    assert boundary.check(tmp_path, ()) == []


def test_cloud_sdk_dependencies_fail_in_python_and_web_packages(tmp_path: Path) -> None:
    _package(tmp_path, "sample", '"google-cloud-storage>=3", "Boto3==1.40"')
    web = tmp_path / "packages" / "webui"
    web.mkdir()
    (web / "package.json").write_text(
        json.dumps(
            {"dependencies": {"react": "19"}, "devDependencies": {"@aws-sdk/client-s3": "3"}}
        )
    )

    assert boundary.check(tmp_path, ()) == [
        "packages/sample/pyproject.toml:3: depends on cloud SDK boto3",
        "packages/sample/pyproject.toml:3: depends on cloud SDK google-cloud-storage",
        "packages/webui/package.json:1: depends on cloud SDK @aws-sdk/client-s3",
    ]


def test_application_packages_cannot_use_platform_code(tmp_path: Path) -> None:
    source = _package(tmp_path, dependencies='"heartwood-reference-platform"')
    (source / "launch.py").write_text("from heartwood_platform.control import client\n")

    errors = boundary.check(tmp_path, ())

    assert errors == [
        "packages/sample/pyproject.toml:3: depends on platform code heartwood-reference-platform",
        "packages/sample/src/heartwood/sample/launch.py:1: imports heartwood_platform.control",
    ]


def test_platform_services_may_use_only_the_shared_schemas(tmp_path: Path) -> None:
    (tmp_path / "packages").mkdir()
    _services(
        tmp_path,
        '"heartwood-schemas", "heartwood-gateway"',
        "from heartwood.schemas import records\n"
        "from heartwood.gateway import app\n"
        "from heartwood import schemas\n",
    )

    assert boundary.check(tmp_path, ()) == [
        "reference-platform/services/pyproject.toml:3: "
        "platform services depend on application package heartwood-gateway",
        "reference-platform/services/src/heartwood_platform/__init__.py:2: "
        "platform services import application package heartwood.gateway",
        "reference-platform/services/src/heartwood_platform/__init__.py:2: "
        "platform services import application package heartwood.gateway.app",
    ]


def test_allowance_accepts_only_its_entry_and_path(tmp_path: Path) -> None:
    source = _package(tmp_path, dependencies='"botocore==1.43"')
    (source / "records.py").write_text("import botocore.session\nimport boto3\n")
    allowlist = (
        boundary.Allowance("packages/sample/pyproject.toml", "botocore", "S3 protocol client"),
        boundary.Allowance("packages/sample/src/*", "botocore", "S3 protocol client"),
    )

    assert boundary.check(tmp_path, allowlist) == [
        "packages/sample/src/heartwood/sample/records.py:2: imports cloud SDK boto3",
    ]


def test_unused_allowance_fails(tmp_path: Path) -> None:
    _package(tmp_path)
    allowlist = (boundary.Allowance("packages/sample/src/*", "botocore", "no longer used"),)

    assert boundary.check(tmp_path, allowlist) == [
        "unused allowance for botocore in packages/sample/src/*"
    ]


def test_unparsable_source_fails_closed(tmp_path: Path) -> None:
    source = _package(tmp_path)
    (source / "broken.py").write_text("import (\n")

    errors = boundary.check(tmp_path, ())

    assert len(errors) == 1
    assert errors[0].startswith(
        "packages/sample/src/heartwood/sample/broken.py:1: cannot be parsed"
    )


def test_coding_declaration_and_stub_files_are_checked(tmp_path: Path) -> None:
    source = _package(tmp_path)
    (source / "legacy.py").write_bytes(
        b"# -*- coding: latin-1 -*-\nimport boto3\nNAME = '\xe9'\nHOST = 'storage.googleapis.com'\n"
    )
    (source / "typed.pyi").write_text("import gcloud.aio.storage\n")

    assert boundary.check(tmp_path, ()) == [
        "packages/sample/src/heartwood/sample/legacy.py:2: imports cloud SDK boto3",
        "packages/sample/src/heartwood/sample/legacy.py:4: "
        "names cloud infrastructure storage.googleapis.com",
        "packages/sample/src/heartwood/sample/typed.pyi:1: imports cloud SDK gcloud.aio.storage",
    ]


def test_files_that_are_not_utf8_are_still_scanned(tmp_path: Path) -> None:
    source = _package(tmp_path)
    (source / "settings.json").write_bytes(b'{"a": "\xff", "host": "s3.amazonaws.com"}\n')

    assert boundary.check(tmp_path, ()) == [
        "packages/sample/src/heartwood/sample/settings.json:1: "
        "names cloud infrastructure s3.amazonaws.com"
    ]
