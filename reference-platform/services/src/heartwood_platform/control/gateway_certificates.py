# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Serving certificates of the session gateways, which the proxy and the Hub verify.

Each gateway pod has the host name ``hw-gw-<session>`` under the headless Service of the
gateways, so its names are ``hw-gw-<session>.<service>.<namespace>.svc`` and the same
name under ``cluster.local``. The init container of the control service creates an
issuing CA from the platform CA with ``python -m heartwood_platform.control.gateway_certificates``.
Its name constraints permit only names under the gateways' Service, so its key cannot sign
a certificate for another platform service. The key of the platform CA stays in the init
container, and the issuing CA is created again at each start of the control service.

The control service signs a new key and certificate for the gateway at each launch of a
session, which the session's gateway Secret carries with the issuing CA's certificate.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# How long a gateway's certificate is valid. A launch issues a new one.
GATEWAY_VALIDITY = timedelta(days=30)
# How long the issuing CA is valid, at most as long as the platform CA.
ISSUER_VALIDITY = timedelta(days=825)
# Tolerance for clocks that differ between the control service and the clients.
_SKEW = timedelta(minutes=5)
CERTIFICATE_FILE = "tls.crt"
KEY_FILE = "tls.key"


def domains(service: str, namespace: str) -> list[str]:
    """Return the DNS domains of the gateways' headless Service."""
    return [f"{service}.{namespace}.svc", f"{service}.{namespace}.svc.cluster.local"]


def gateway_host(session: str) -> str:
    """Return the host name of a session's gateway pod, the name of its ServiceAccount."""
    return f"hw-gw-{session}"


def _pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def _key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _authority_key_identifier(authority: x509.Certificate) -> x509.AuthorityKeyIdentifier:
    """Name the key of ``authority`` as its own subject key identifier names it."""
    try:
        identifier = authority.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
    except x509.ExtensionNotFound:
        return x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.public_key())  # type: ignore[arg-type]
    return x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(identifier.value)


def create_issuer(
    authority_certificate: bytes,
    authority_key: bytes,
    service: str,
    namespace: str,
    now: datetime,
) -> tuple[str, str]:
    """Return the certificate and key of an issuing CA for the gateways, in PEM.

    The platform CA signs it. It may sign only end-entity certificates for DNS names under
    the gateways' Service, and none for an IP address.
    """
    authority = x509.load_pem_x509_certificate(authority_certificate)
    signer = serialization.load_pem_private_key(authority_key, password=None)
    if not isinstance(signer, ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey):
        raise ValueError("the platform CA key is neither an RSA nor an EC key")
    key = ec.generate_private_key(ec.SECP256R1())
    expires = min(now + ISSUER_VALIDITY, authority.not_valid_after_utc)
    if expires <= now:
        raise ValueError("the platform CA has expired")
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Heartwood Gateway CA")]))
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _SKEW)
        .not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        # The issuing CA signs serving certificates only, so none of its certificates is
        # a client certificate that the Hub accepts.
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.DNSName(name) for name in domains(service, namespace)],
                excluded_subtrees=[
                    x509.IPAddress(ipaddress.ip_network("0.0.0.0/0")),
                    x509.IPAddress(ipaddress.ip_network("::/0")),
                ],
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(_authority_key_identifier(authority), critical=False)
        .sign(signer, hashes.SHA256())
    )
    return _pem(certificate), _key_pem(key)


class GatewayCertificates:
    """Sign the serving certificates of the session gateways with the issuing CA."""

    def __init__(
        self,
        directory: Path,
        service: str,
        namespace: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Read the issuing CA that the init container wrote to ``directory``."""
        self._certificate_pem = (directory / CERTIFICATE_FILE).read_text(encoding="ascii")
        self._certificate = x509.load_pem_x509_certificate(self._certificate_pem.encode())
        key = serialization.load_pem_private_key((directory / KEY_FILE).read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("the issuing CA key is not an EC key")
        self._key = key
        self._domains = domains(service, namespace)
        self._clock = clock

    def names(self, session: str) -> list[str]:
        """Return the DNS names of a session's gateway."""
        return [f"{gateway_host(session)}.{domain}" for domain in self._domains]

    def issue(self, session: str) -> dict[str, str]:
        """Return a new key and certificate of a session's gateway, by Secret key.

        The certificate file holds the gateway's certificate and the issuing CA's, so a
        client that trusts the platform CA alone verifies the chain.
        """
        now = self._clock()
        expires = min(now + GATEWAY_VALIDITY, self._certificate.not_valid_after_utc)
        if expires <= now:
            raise ValueError("the issuing CA of the gateways has expired")
        names = self.names(session)
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, gateway_host(session))])
            )
            .issuer_name(self._certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _SKEW)
            .not_valid_after(expires)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(name) for name in names]),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(_authority_key_identifier(self._certificate), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        return {
            "gateway-tls.crt": _pem(certificate) + self._certificate_pem,
            "gateway-tls.key": _key_pem(key),
        }


def main(argv: list[str] | None = None) -> None:
    """Write the issuing CA of the gateways, signed by the platform CA, to a directory."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("authority", type=Path, help="Directory with tls.crt and tls.key")
    parser.add_argument("service", help="The headless Service of the gateway pods")
    parser.add_argument("namespace", help="The namespace of the gateway pods")
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args(argv)
    certificate, key = create_issuer(
        (arguments.authority / CERTIFICATE_FILE).read_bytes(),
        (arguments.authority / KEY_FILE).read_bytes(),
        arguments.service,
        arguments.namespace,
        datetime.now(UTC),
    )
    # An init container runs again when its pod's sandbox is recreated, so the files are
    # replaced: the key first, so a certificate is never newer than its key.
    for name, content, mode in ((KEY_FILE, key, 0o600), (CERTIFICATE_FILE, certificate, 0o644)):
        partial = arguments.output / f".{name}.partial"
        partial.unlink(missing_ok=True)
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="ascii") as file:
            file.write(content)
        partial.replace(arguments.output / name)


if __name__ == "__main__":
    main()
