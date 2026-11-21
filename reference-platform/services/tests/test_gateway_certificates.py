# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""The serving certificates of the session gateways and the CA that signs them."""

from __future__ import annotations

import base64
import ipaddress
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from heartwood_platform.control.gateway_certificates import (
    GATEWAY_VALIDITY,
    GatewayCertificates,
    create_issuer,
    main,
)
from test_chart import _CHART, _TEMPLATE, _named

_NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
_SESSION = "0123456789abcdef"
_NAMES = [
    f"hw-gw-{_SESSION}.hw-gateways.heartwood-sessions.svc",
    f"hw-gw-{_SESSION}.hw-gateways.heartwood-sessions.svc.cluster.local",
]


@pytest.fixture(scope="module")
def authority() -> tuple[bytes, bytes]:
    """The platform CA of a rendered chart, as Helm creates it."""
    output = subprocess.run(
        [*_TEMPLATE, "--values", str(_CHART / "values-kind.yaml")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(output) if item is not None]
    data = _named(objects, "Secret", "platform-ca")["data"]
    return base64.b64decode(data["tls.crt"]), base64.b64decode(data["tls.key"])


def synthetic_authority() -> tuple[bytes, bytes]:
    """A synthetic platform CA, for tests that render no chart."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic Platform CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOW - timedelta(days=1))
        .not_valid_after(_NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def issuer_directory(directory: Path, authority: tuple[bytes, bytes], now: datetime) -> Path:
    """Write an issuing CA for the gateways of heartwood-sessions to ``directory``."""
    certificate, key = create_issuer(*authority, "hw-gateways", "heartwood-sessions", now)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tls.crt").write_text(certificate, encoding="ascii")
    (directory / "tls.key").write_text(key, encoding="ascii")
    return directory


def _verify(
    authority: bytes,
    chain: str,
    tmp_path: Path,
    name: str | None = None,
    purpose: str = "sslserver",
) -> Any:  # noqa: ANN401
    """Verify a PEM chain, the end-entity certificate first, against the platform CA alone."""
    root = tmp_path / "root.crt"
    root.write_bytes(authority)
    first, _, rest = chain.partition("-----END CERTIFICATE-----\n")
    leaf, untrusted = tmp_path / "leaf.crt", tmp_path / "untrusted.crt"
    leaf.write_text(first + "-----END CERTIFICATE-----\n", encoding="ascii")
    untrusted.write_text(rest, encoding="ascii")
    command = ["openssl", "verify", "-x509_strict", "-CAfile", str(root)]
    command += ["-untrusted", str(untrusted), "-purpose", purpose]
    if name is not None:
        command += ["-verify_hostname", name]
    return subprocess.run([*command, str(leaf)], capture_output=True, text=True, check=False)


def test_a_launch_receives_a_certificate_for_its_names_that_chains_to_the_platform_ca(
    authority: tuple[bytes, bytes], tmp_path: Path
) -> None:
    certificates = GatewayCertificates(
        issuer_directory(tmp_path / "issuer", authority, _NOW),
        "hw-gateways",
        "heartwood-sessions",
        clock=lambda: _NOW,
    )
    files = certificates.issue(_SESSION)
    assert set(files) == {"gateway-tls.crt", "gateway-tls.key"}
    for name in _NAMES:
        result = _verify(authority[0], files["gateway-tls.crt"], tmp_path, name)
        assert result.returncode == 0, result.stdout + result.stderr
    assert _verify(authority[0], files["gateway-tls.crt"], tmp_path, "hub").returncode != 0
    leaf = x509.load_pem_x509_certificate(files["gateway-tls.crt"].encode())
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == _NAMES
    assert leaf.not_valid_after_utc == _NOW + GATEWAY_VALIDITY
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    key = serialization.load_pem_private_key(files["gateway-tls.key"].encode(), password=None)
    assert key.public_key().public_numbers() == leaf.public_key().public_numbers()  # type: ignore[union-attr]
    # Each launch receives a new key.
    again = certificates.issue(_SESSION)
    assert again["gateway-tls.key"] != files["gateway-tls.key"]
    assert again["gateway-tls.crt"] != files["gateway-tls.crt"]


def _forged(issuer: Path, name: x509.GeneralName) -> str:
    """Sign a certificate for another name with the issuing CA's key."""
    certificate = x509.load_pem_x509_certificate((issuer / "tls.crt").read_bytes())
    key = serialization.load_pem_private_key((issuer / "tls.key").read_bytes(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    forged = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "forged")]))
        .issuer_name(certificate.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([name]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return (
        forged.public_bytes(serialization.Encoding.PEM).decode()
        + certificate.public_bytes(serialization.Encoding.PEM).decode()
    )


@pytest.mark.parametrize(
    "name",
    [
        x509.DNSName("keycloak.heartwood-platform.svc.cluster.local"),
        x509.DNSName("hub"),
        x509.DNSName("hw-gateways.heartwood-sessions.svc.cluster.local.example.test"),
        x509.DNSName("hw-gw-x.other.heartwood-sessions.svc.cluster.local"),
        x509.IPAddress(ipaddress.ip_address("10.0.0.5")),
    ],
)
def test_the_issuing_ca_cannot_sign_a_name_outside_the_gateways_service(
    authority: tuple[bytes, bytes], tmp_path: Path, name: x509.GeneralName
) -> None:
    issuer = issuer_directory(tmp_path / "issuer", authority, datetime.now(UTC))
    result = _verify(authority[0], _forged(issuer, name), tmp_path)
    assert result.returncode != 0
    assert "subtree" in (result.stdout + result.stderr)
    # A name under the Service passes, so the refusal is the name constraint's.
    permitted = x509.DNSName("hw-gw-x.hw-gateways.heartwood-sessions.svc.cluster.local")
    assert _verify(authority[0], _forged(issuer, permitted), tmp_path).returncode == 0
    # No certificate of the issuing CA serves as a client certificate.
    forged = _forged(issuer, permitted)
    assert _verify(authority[0], forged, tmp_path, purpose="sslclient").returncode != 0


def test_the_issuing_ca_lives_no_longer_than_the_platform_ca(
    authority: tuple[bytes, bytes], tmp_path: Path
) -> None:
    root = x509.load_pem_x509_certificate(authority[0])
    late = root.not_valid_after_utc - timedelta(days=10)
    issuer = issuer_directory(tmp_path / "issuer", authority, late)
    certificate = x509.load_pem_x509_certificate((issuer / "tls.crt").read_bytes())
    assert certificate.not_valid_after_utc == root.not_valid_after_utc
    usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical
    assert (constraints.value.ca, constraints.value.path_length) == (True, 0)
    # A gateway's certificate ends with its issuing CA.
    certificates = GatewayCertificates(
        issuer, "hw-gateways", "heartwood-sessions", clock=lambda: late
    )
    leaf = x509.load_pem_x509_certificate(certificates.issue(_SESSION)["gateway-tls.crt"].encode())
    assert leaf.not_valid_after_utc == root.not_valid_after_utc
    expired = GatewayCertificates(
        issuer, "hw-gateways", "heartwood-sessions", clock=lambda: late + timedelta(days=11)
    )
    with pytest.raises(ValueError, match="expired"):
        expired.issue(_SESSION)


def test_the_init_container_writes_the_issuing_ca_with_an_owner_only_key(
    authority: tuple[bytes, bytes], tmp_path: Path
) -> None:
    source, output = tmp_path / "platform-ca", tmp_path / "issuer"
    source.mkdir()
    output.mkdir()
    (source / "tls.crt").write_bytes(authority[0])
    (source / "tls.key").write_bytes(authority[1])
    main([str(source), "hw-gateways", "heartwood-sessions", str(output)])
    assert (output / "tls.key").stat().st_mode & 0o777 == 0o600
    issued = GatewayCertificates(output, "hw-gateways", "heartwood-sessions").issue(_SESSION)
    assert _verify(authority[0], issued["gateway-tls.crt"], tmp_path, _NAMES[1]).returncode == 0
    # A rerun of the init container replaces the issuing CA and its key together.
    first = (output / "tls.key").read_bytes()
    (output / ".tls.key.partial").write_text("left by an interrupted run", encoding="ascii")
    main([str(source), "hw-gateways", "heartwood-sessions", str(output)])
    assert (output / "tls.key").read_bytes() != first
    assert (output / "tls.key").stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in output.iterdir()) == ["tls.crt", "tls.key"]
    issued = GatewayCertificates(output, "hw-gateways", "heartwood-sessions").issue(_SESSION)
    assert _verify(authority[0], issued["gateway-tls.crt"], tmp_path, _NAMES[1]).returncode == 0


def test_only_the_init_container_of_the_control_service_reads_the_platform_ca_key() -> None:
    output = subprocess.run(
        [*_TEMPLATE, "--values", str(_CHART / "values-kind.yaml")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    objects = [item for item in yaml.safe_load_all(output) if item is not None]
    spec = _named(objects, "StatefulSet", "heartwood-control")["spec"]["template"]["spec"]
    (init,) = spec["initContainers"]
    assert init["args"] == [
        "heartwood_platform.control.gateway_certificates",
        "/etc/platform-ca",
        "hw-gateways",
        "heartwood-sessions",
        "/var/run/heartwood-control/gateway-issuer",
    ]
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    keys = [
        name
        for name, volume in volumes.items()
        if volume.get("secret", {}).get("secretName") == "platform-ca"
    ]
    assert keys == ["platform-ca-key"]
    assert "platform-ca-key" in {mount["name"] for mount in init["volumeMounts"]}
    # Only the init container mounts the key of the platform CA.
    for container in spec["containers"]:
        assert "platform-ca-key" not in {mount["name"] for mount in container["volumeMounts"]}
        mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        assert mounts["gateway-issuer"]["readOnly"] is True
    assert volumes["gateway-issuer"]["emptyDir"]["medium"] == "Memory"
    configuration = json.loads(
        _named(objects, "ConfigMap", "heartwood-control")["data"]["configuration.json"]
    )
    assert configuration["gateway_tls"] == {
        "issuer_directory": "/var/run/heartwood-control/gateway-issuer",
        "service": "hw-gateways",
    }
