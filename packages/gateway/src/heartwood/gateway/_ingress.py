# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Strict gateway ingress configuration and request normalization."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal, cast
from urllib.parse import SplitResult, urlsplit

from heartwood.adapters import INGRESS_MODES, IngressMode

type PrefixHandling = Literal["preserve", "strip"]

_HTTP_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_SUPPORTED_FORWARDED_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-prefix",
        "x-forwarded-proto",
        "x-original-url",
        "x-rewrite-url",
    }
)
_UNSUPPORTED_FORWARDED_HEADERS = frozenset(
    {
        "x-forwarded-port",
        "x-forwarded-server",
        "x-forwarded-uri",
        "x-real-ip",
    }
)
_JUPYTER_CONTEXT_HEADERS = frozenset(
    {
        "x-forwarded-context",
        "x-proxycontextpath",
    }
)
_FORWARDED_HEADERS = (
    _SUPPORTED_FORWARDED_HEADERS | _UNSUPPORTED_FORWARDED_HEADERS | _JUPYTER_CONTEXT_HEADERS
)
_SENSITIVE_SINGLETON_HEADERS = _FORWARDED_HEADERS | {
    "host",
    "origin",
    "sec-fetch-site",
}


class IngressConfigurationError(ValueError):
    """Raised when a gateway deployment does not define a safe ingress route."""


class IngressRequestError(ValueError):
    """Raised when one request violates the configured ingress route."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class IngressRequest:
    """One validated request translated to the gateway's internal route."""

    path: str
    query_string: str
    external_origin: str
    external_base_path: str
    client_ip: str


@dataclass(frozen=True, slots=True)
class IngressPolicy:
    """One explicit route from a browser or API client to the gateway."""

    mode: IngressMode
    bind_host: str
    bind_port: int
    external_origin: str
    external_base_path: str = "/"
    prefix_handling: PrefixHandling = "preserve"
    trusted_proxy_sources: tuple[str, ...] = ()
    trusted_identity_header: str | None = None
    trusted_identity: str | None = None
    host_loopback_publication: bool = False

    @classmethod
    def create(
        cls,
        *,
        mode: str = "direct-loopback",
        bind_host: str = "127.0.0.1",
        bind_port: int = 8767,
        external_origin: str | None = None,
        external_base_path: str = "/",
        prefix_handling: str | None = None,
        trusted_proxy_sources: Sequence[str] = (),
        trusted_identity_header: str | None = None,
        trusted_identity: str | None = None,
        host_loopback_publication: bool = False,
    ) -> IngressPolicy:
        """Validate and normalize one deployment-owned ingress configuration."""
        if mode not in INGRESS_MODES:
            raise IngressConfigurationError(f"unsupported gateway ingress mode: {mode}")
        if not 1 <= bind_port <= 65_535:
            raise IngressConfigurationError("gateway bind port must be between 1 and 65535")
        normalized_host, loopback_bind, wildcard_bind = _normalize_bind_host(bind_host)
        if (
            mode == "direct-loopback"
            and not loopback_bind
            and not (host_loopback_publication and wildcard_bind)
        ):
            raise IngressConfigurationError(
                "direct-loopback ingress requires a loopback bind; a container may use "
                "a wildcard bind only with an explicit host-loopback publication boundary"
            )
        if mode == "jupyter-proxy" and not loopback_bind:
            raise IngressConfigurationError(
                "Jupyter proxy ingress requires a loopback gateway bind"
            )
        if mode in {"jupyter-proxy", "trusted-proxy"} and not external_origin:
            raise IngressConfigurationError(f"{mode} ingress requires the exact external origin")
        normalized_base = normalize_base_path(external_base_path)
        normalized_origin = _normalize_origin(
            external_origin
            or (
                f"http://{_authority(normalized_host, bind_port)}"
                if loopback_bind
                else f"http://127.0.0.1:{bind_port}"
                if host_loopback_publication and wildcard_bind
                else ""
            )
        )
        normalized_prefix_handling: PrefixHandling
        if prefix_handling is None:
            normalized_prefix_handling = "strip" if mode == "jupyter-proxy" else "preserve"
        elif prefix_handling in {"preserve", "strip"}:
            normalized_prefix_handling = cast(PrefixHandling, prefix_handling)
        else:
            raise IngressConfigurationError("gateway prefix handling must be preserve or strip")

        sources = _normalize_proxy_sources(trusted_proxy_sources)
        identity_header, identity = _normalize_proxy_identity(
            trusted_identity_header,
            trusted_identity,
        )
        if mode == "direct-loopback":
            if not _origin_is_loopback(normalized_origin):
                raise IngressConfigurationError(
                    "direct-loopback ingress requires a loopback external origin"
                )
            if sources or identity_header is not None:
                raise IngressConfigurationError(
                    "direct-loopback ingress cannot declare trusted proxy metadata"
                )
            if normalized_prefix_handling != "preserve":
                raise IngressConfigurationError(
                    "direct-loopback ingress must preserve its configured base path"
                )
        elif mode == "jupyter-proxy":
            if normalized_base == "/":
                raise IngressConfigurationError(
                    "Jupyter proxy ingress requires the exact external proxy base path"
                )
            if sources or identity_header is not None:
                raise IngressConfigurationError(
                    "Jupyter proxy ingress uses the loopback proxy boundary and cannot configure "
                    "trusted-proxy source or identity assertions"
                )
            if normalized_prefix_handling != "strip":
                raise IngressConfigurationError(
                    "Jupyter proxy ingress must strip its external prefix before the gateway"
                )
        else:
            if not sources:
                raise IngressConfigurationError(
                    "trusted-proxy ingress requires at least one exact proxy source range"
                )
            if host_loopback_publication:
                raise IngressConfigurationError(
                    "trusted-proxy ingress cannot use the host-loopback publication boundary"
                )

        return cls(
            mode=mode,
            bind_host=normalized_host,
            bind_port=bind_port,
            external_origin=normalized_origin,
            external_base_path=normalized_base,
            prefix_handling=normalized_prefix_handling,
            trusted_proxy_sources=sources,
            trusted_identity_header=identity_header,
            trusted_identity=identity,
            host_loopback_publication=host_loopback_publication,
        )

    def safe_dict(self) -> dict[str, object]:
        """Return the non-secret ingress configuration."""
        configuration = asdict(self)
        configuration.pop("trusted_identity")
        configuration["trusted_identity_configured"] = self.trusted_identity is not None
        return configuration

    @property
    def browser_base_path(self) -> str:
        """Return the browser client base path, with root represented as empty."""
        return "" if self.external_base_path == "/" else self.external_base_path

    def validate_scope(
        self,
        scope: Mapping[str, object],
        *,
        websocket: bool = False,
    ) -> IngressRequest:
        """Validate one HTTP or WebSocket ASGI scope and normalize its path once."""
        headers = _headers(scope)
        source_ip = _client_ip(scope)
        self._validate_source(source_ip)
        forwarded_client = self._validate_forwarding(headers)
        self._validate_host(headers)
        self._validate_origin(headers, websocket=websocket)
        path = _validated_path(scope)
        query_string = _validated_query_string(scope)
        internal_path = self._internal_path(path)
        return IngressRequest(
            path=internal_path,
            query_string=query_string,
            external_origin=self.external_origin,
            external_base_path=self.external_base_path,
            client_ip=forwarded_client or source_ip,
        )

    def _validate_source(self, client_ip: str) -> None:
        address = ipaddress.ip_address(client_ip)
        if self.mode == "direct-loopback":
            allowed = address.is_loopback or (
                self.host_loopback_publication and (address.is_private or address.is_link_local)
            )
            if not allowed:
                raise IngressRequestError(
                    "direct request did not originate from the configured local boundary",
                    status_code=403,
                )
        if self.mode == "jupyter-proxy" and not address.is_loopback:
            raise IngressRequestError(
                "Jupyter proxy request did not originate from loopback",
                status_code=403,
            )
        if self.mode != "trusted-proxy":
            return
        networks = tuple(ipaddress.ip_network(source) for source in self.trusted_proxy_sources)
        if not any(address in network for network in networks):
            raise IngressRequestError(
                "request did not originate from a trusted proxy source",
                status_code=403,
            )

    def _validate_forwarding(
        self,
        headers: Mapping[str, tuple[str, ...]],
    ) -> str | None:
        forwarded = _FORWARDED_HEADERS.intersection(headers)
        if self.mode == "direct-loopback":
            if forwarded:
                raise IngressRequestError(
                    "forwarded metadata is not accepted by this ingress mode",
                    status_code=403,
                )
            return None
        _reject_ambiguous_forwarding(forwarded)
        if self.mode == "jupyter-proxy":
            self._validate_jupyter_forwarding(headers, forwarded)
            return None
        unsupported = forwarded.intersection(
            _UNSUPPORTED_FORWARDED_HEADERS | _JUPYTER_CONTEXT_HEADERS
        )
        if unsupported:
            raise IngressRequestError(
                f"unsupported forwarded metadata is not accepted: {', '.join(sorted(unsupported))}"
            )
        required = {
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-prefix",
            "x-forwarded-proto",
        }
        if not required.issubset(forwarded):
            raise IngressRequestError(
                "trusted proxy metadata must include client, host, protocol, and prefix"
            )
        forwarded_client = _forwarded_clients(headers, allow_chain=False)[0]
        expected = urlsplit(self.external_origin)
        if _normalize_authority(_one_header(headers, "x-forwarded-host")) != _origin_authority(
            expected
        ):
            raise IngressRequestError(
                "forwarded host does not match the configured external origin",
                status_code=403,
            )
        if _one_header(headers, "x-forwarded-proto").lower() != expected.scheme:
            raise IngressRequestError(
                "forwarded protocol does not match the configured external origin",
                status_code=403,
            )
        try:
            forwarded_prefix = normalize_base_path(_one_header(headers, "x-forwarded-prefix"))
        except IngressConfigurationError as error:
            raise IngressRequestError("forwarded prefix is malformed") from error
        if forwarded_prefix != self.external_base_path:
            raise IngressRequestError(
                "forwarded prefix does not match the configured external base path",
                status_code=403,
            )
        if self.trusted_identity_header is not None:
            identity = _one_header(headers, self.trusted_identity_header)
            if identity != self.trusted_identity:
                raise IngressRequestError(
                    "trusted proxy identity does not match the configured identity",
                    status_code=403,
                )
        return forwarded_client

    def _validate_jupyter_forwarding(
        self,
        headers: Mapping[str, tuple[str, ...]],
        forwarded: frozenset[str],
    ) -> None:
        unsupported = forwarded.intersection(_UNSUPPORTED_FORWARDED_HEADERS)
        if unsupported:
            raise IngressRequestError(
                f"unsupported forwarded metadata is not accepted: {', '.join(sorted(unsupported))}"
            )
        context_headers = forwarded.intersection(_JUPYTER_CONTEXT_HEADERS)
        if context_headers and not (_JUPYTER_CONTEXT_HEADERS | {"x-forwarded-prefix"}).issubset(
            forwarded
        ):
            raise IngressRequestError(
                "Jupyter proxy context metadata must include context, proxy context, and prefix"
            )
        route_headers = forwarded.intersection({"x-forwarded-host", "x-forwarded-proto"})
        if route_headers and route_headers != {"x-forwarded-host", "x-forwarded-proto"}:
            raise IngressRequestError(
                "Jupyter forwarded route metadata must include host and protocol"
            )
        if "x-forwarded-for" in forwarded:
            _forwarded_clients(headers, allow_chain=True)
        expected = urlsplit(self.external_origin)
        if "x-forwarded-host" in forwarded and _normalize_authority(
            _one_header(headers, "x-forwarded-host")
        ) != _origin_authority(expected):
            raise IngressRequestError(
                "forwarded host does not match the configured external origin",
                status_code=403,
            )
        if (
            "x-forwarded-proto" in forwarded
            and _one_header(headers, "x-forwarded-proto").lower() != expected.scheme
        ):
            raise IngressRequestError(
                "forwarded protocol does not match the configured external origin",
                status_code=403,
            )
        for name in (*sorted(_JUPYTER_CONTEXT_HEADERS), "x-forwarded-prefix"):
            if name not in forwarded:
                continue
            try:
                value = normalize_base_path(_one_header(headers, name))
            except IngressConfigurationError as error:
                raise IngressRequestError(f"{name} is malformed") from error
            if value != self.external_base_path:
                raise IngressRequestError(
                    f"{name} does not match the configured external base path",
                    status_code=403,
                )

    def _validate_host(self, headers: Mapping[str, tuple[str, ...]]) -> None:
        host = _normalize_authority(_one_header(headers, "host"))
        external = _origin_authority(urlsplit(self.external_origin))
        bind = _authority(self.bind_host, self.bind_port)
        allowed = {external, bind}
        if self.bind_host in {"0.0.0.0", "::"}:
            allowed.discard(bind)
        if host not in allowed:
            raise IngressRequestError(
                "request host does not match the configured ingress route",
                status_code=403,
            )

    def _validate_origin(
        self,
        headers: Mapping[str, tuple[str, ...]],
        *,
        websocket: bool,
    ) -> None:
        origins = headers.get("origin", ())
        if websocket and not origins:
            raise IngressRequestError(
                "WebSocket requests require an Origin header",
                status_code=403,
            )
        if origins:
            try:
                origin = _normalize_origin(_one_header(headers, "origin"))
            except IngressConfigurationError as error:
                raise IngressRequestError(
                    "request origin is malformed",
                    status_code=403,
                ) from error
            if origin != self.external_origin:
                raise IngressRequestError(
                    "request origin does not match the configured ingress route",
                    status_code=403,
                )
        fetch_site = headers.get("sec-fetch-site", ())
        if fetch_site:
            value = _one_header(headers, "sec-fetch-site").lower()
            if value not in {"none", "same-origin"}:
                raise IngressRequestError(
                    "cross-origin browser requests are not accepted",
                    status_code=403,
                )

    def _internal_path(self, path: str) -> str:
        base = self.external_base_path
        if self.prefix_handling == "strip":
            if base != "/" and (path == base or path.startswith(f"{base}/")):
                raise IngressRequestError(
                    "request contains a proxy prefix that must be stripped before the gateway"
                )
            return path
        if base == "/":
            return path
        if path == base:
            return "/"
        prefix = f"{base}/"
        if not path.startswith(prefix):
            raise IngressRequestError(
                "request path is outside the configured gateway base path",
                status_code=404,
            )
        return f"/{path[len(prefix) :]}"


def normalize_base_path(value: str) -> str:
    """Return one canonical absolute URL path or reject ambiguous input."""
    if not isinstance(value, str):
        raise IngressConfigurationError("gateway base path must be a string")
    if value == "/":
        return "/"
    if not value or not value.startswith("/"):
        raise IngressConfigurationError("gateway base path must start with /")
    if value.endswith("/"):
        value = value[:-1]
    try:
        _validate_path_text(value)
    except IngressRequestError as error:
        raise IngressConfigurationError(str(error)) from error
    return value


def _normalize_bind_host(value: str) -> tuple[str, bool, bool]:
    normalized = value.strip().lower()
    if normalized == "localhost":
        return "127.0.0.1", True, False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as error:
        raise IngressConfigurationError(
            "gateway bind host must be a literal loopback, wildcard, or proxy-facing IP address"
        ) from error
    return str(address), address.is_loopback, address.is_unspecified


def _normalize_origin(value: str) -> str:
    if not value:
        raise IngressConfigurationError("gateway external origin must be configured")
    if any(character.isspace() for character in value):
        raise IngressConfigurationError("gateway external origin cannot contain whitespace")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or "*" in parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise IngressConfigurationError(
            "gateway external origin must contain only an http(s) scheme and authority"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise IngressConfigurationError("gateway external origin has an invalid port") from error
    default_port = 80 if parsed.scheme == "http" else 443
    authority = (
        _authority(parsed.hostname, port) if port != default_port else _host(parsed.hostname)
    )
    return f"{parsed.scheme.lower()}://{authority}"


def _origin_is_loopback(origin: str) -> bool:
    hostname = urlsplit(origin).hostname
    if hostname is None:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _normalize_proxy_sources(values: Sequence[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in values:
        try:
            network = ipaddress.ip_network(value.strip(), strict=False)
        except ValueError as error:
            raise IngressConfigurationError(
                f"invalid trusted proxy source range: {value}"
            ) from error
        if network.prefixlen == 0:
            raise IngressConfigurationError("wildcard trusted proxy source ranges are forbidden")
        normalized.add(str(network))
    return tuple(sorted(normalized))


def _normalize_proxy_identity(
    header: str | None,
    identity: str | None,
) -> tuple[str | None, str | None]:
    if (header is None) != (identity is None):
        raise IngressConfigurationError(
            "trusted proxy identity header and value must be configured together"
        )
    if header is None or identity is None:
        return None, None
    normalized_header = header.strip().lower()
    if (
        _HTTP_TOKEN.fullmatch(normalized_header) is None
        or normalized_header in _SENSITIVE_SINGLETON_HEADERS
        or normalized_header in {"authorization", "cookie"}
    ):
        raise IngressConfigurationError("trusted proxy identity header is invalid or reserved")
    normalized_identity = identity.strip()
    if (
        not normalized_identity
        or len(normalized_identity) > 512
        or any(character in normalized_identity for character in "\r\n\0")
    ):
        raise IngressConfigurationError("trusted proxy identity value is invalid")
    return normalized_header, normalized_identity


def _headers(scope: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    raw = scope.get("headers", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise IngressRequestError("ASGI request headers are malformed")
    values: dict[str, list[str]] = {}
    for item in raw:
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes))
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            raise IngressRequestError("ASGI request headers are malformed")
        try:
            name = item[0].decode("ascii").lower()
            value = item[1].decode("latin-1")
        except UnicodeDecodeError as error:
            raise IngressRequestError("request header name is not ASCII") from error
        values.setdefault(name, []).append(value)
    result = {name: tuple(items) for name, items in values.items()}
    duplicates = sorted(
        name for name in _SENSITIVE_SINGLETON_HEADERS if len(result.get(name, ())) > 1
    )
    if duplicates:
        raise IngressRequestError(
            f"security-sensitive request headers must not repeat: {', '.join(duplicates)}"
        )
    return result


def _one_header(headers: Mapping[str, tuple[str, ...]], name: str) -> str:
    values = headers.get(name, ())
    if len(values) != 1 or not values[0].strip():
        raise IngressRequestError(f"request requires one {name} header")
    value = values[0].strip()
    if any(character in value for character in "\r\n\0"):
        raise IngressRequestError(f"request {name} header is malformed")
    return value


def _reject_ambiguous_forwarding(forwarded: frozenset[str]) -> None:
    if "forwarded" in forwarded:
        raise IngressRequestError(
            "RFC Forwarded metadata is unsupported; configure one X-Forwarded header set"
        )
    if forwarded.intersection({"x-original-url", "x-rewrite-url"}):
        raise IngressRequestError("rewritten URL metadata is not accepted")


def _forwarded_clients(
    headers: Mapping[str, tuple[str, ...]],
    *,
    allow_chain: bool,
) -> tuple[str, ...]:
    raw_clients = _one_header(headers, "x-forwarded-for").split(",")
    if not allow_chain and len(raw_clients) != 1:
        raise IngressRequestError("forwarded client must contain one address")
    if not 1 <= len(raw_clients) <= 8:
        raise IngressRequestError("forwarded client chain is too long")
    try:
        return tuple(str(ipaddress.ip_address(value.strip())) for value in raw_clients)
    except ValueError as error:
        raise IngressRequestError("forwarded client is not a valid IP address") from error


def _client_ip(scope: Mapping[str, object]) -> str:
    client = scope.get("client")
    if not isinstance(client, tuple) or len(client) != 2 or not isinstance(client[0], str):
        raise IngressRequestError("gateway could not identify the request source", status_code=403)
    try:
        return str(ipaddress.ip_address(client[0]))
    except ValueError as error:
        raise IngressRequestError(
            "gateway request source is not a valid IP address",
            status_code=403,
        ) from error


def _validated_path(scope: Mapping[str, object]) -> str:
    path = scope.get("path")
    raw_path = scope.get("raw_path")
    root_path = scope.get("root_path", "")
    if root_path not in {"", None}:
        raise IngressRequestError(
            "ASGI root path is unsupported; configure the gateway ingress prefix once"
        )
    if not isinstance(path, str) or not path.startswith("/"):
        raise IngressRequestError("request path must be absolute")
    if raw_path is None:
        raw_path = path.encode("utf-8")
    if not isinstance(raw_path, bytes):
        raise IngressRequestError("raw request path is malformed")
    if b"%" in raw_path:
        raise IngressRequestError("percent-encoded gateway paths are not accepted")
    try:
        raw_text = raw_path.decode("utf-8")
    except UnicodeDecodeError as error:
        raise IngressRequestError("request path is not valid UTF-8") from error
    if raw_text != path:
        raise IngressRequestError("decoded and raw request paths do not agree")
    _validate_path_text(path)
    return path


def _validated_query_string(scope: Mapping[str, object]) -> str:
    raw_query = scope.get("query_string", b"")
    if not isinstance(raw_query, bytes):
        raise IngressRequestError("raw request query is malformed")
    if len(raw_query) > 8_192:
        raise IngressRequestError("request query is too long")
    try:
        query = raw_query.decode("utf-8")
    except UnicodeDecodeError as error:
        raise IngressRequestError("request query is not valid UTF-8") from error
    if any(character in query for character in "\r\n\0#"):
        raise IngressRequestError("request query contains a forbidden character")
    return query


def _validate_path_text(path: str) -> None:
    if any(character in path for character in ("\\", "\0", "?", "#")):
        raise IngressRequestError("gateway path contains a forbidden character")
    if "//" in path:
        raise IngressRequestError("gateway path contains an empty segment")
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise IngressRequestError("gateway path contains a traversal segment")


def _normalize_authority(value: str) -> str:
    if any(character.isspace() for character in value) or any(
        character in value for character in ("/", "\\", "@", ",", "#", "?")
    ):
        raise IngressRequestError("request authority is malformed")
    parsed = urlsplit(f"//{value}")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise IngressRequestError("request authority is malformed")
    try:
        port = parsed.port
    except ValueError as error:
        raise IngressRequestError("request authority has an invalid port") from error
    return _authority(parsed.hostname, port)


def _origin_authority(origin: SplitResult) -> str:
    try:
        port = origin.port
    except ValueError as error:  # pragma: no cover - origin was validated at construction
        raise IngressConfigurationError("gateway external origin has an invalid port") from error
    return _authority(cast(str, origin.hostname), port)


def _authority(host: str, port: int | None) -> str:
    normalized = _host(host)
    return normalized if port is None else f"{normalized}:{port}"


def _host(value: str) -> str:
    normalized = value.lower()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    return f"[{address}]" if address.version == 6 else str(address)
