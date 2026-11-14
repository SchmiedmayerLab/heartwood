/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

const capabilityKey = (basePath: string): string => {
  const url = new URL(joinPath(basePath, "/"), window.location.href);
  if (url.origin !== window.location.origin) {
    throw new Error("The gateway must use this page's origin.");
  }
  return `heartwood.gateway-capability:${url.pathname}`;
};

export const gatewayCapability = (basePath: string): string | null =>
  window.sessionStorage.getItem(capabilityKey(basePath));

export const gatewayRequestUrl = (
  basePath: string,
  input: RequestInfo | URL,
): URL => {
  const target = new URL(
    typeof input === "string" ? input
    : input instanceof URL ? input.href
    : input.url,
    window.location.href,
  );
  if (target.origin !== window.location.origin) {
    throw new Error("The gateway must use this page's origin.");
  }
  const base = new URL(joinPath(basePath, "/"), window.location.href);
  if (!target.pathname.startsWith(base.pathname)) {
    throw new Error("Gateway requests must stay within their base path.");
  }
  return target;
};

export const authenticatedFetch = (
  basePath: string,
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> => {
  gatewayRequestUrl(basePath, input);
  const headers = new Headers(
    init?.headers ?? (input instanceof Request ? input.headers : undefined),
  );
  const capability = gatewayCapability(basePath);
  if (capability !== null) {
    headers.set("X-Heartwood-Capability", capability);
  }
  return fetch(input, {
    ...init,
    headers,
    credentials: "same-origin",
    redirect: "error",
  });
};

export const initializeGatewayAccess = async (): Promise<void> => {
  const token = new URLSearchParams(window.location.hash.slice(1)).get(
    "launch",
  );
  if (token === null) {
    return;
  }
  window.history.replaceState(
    null,
    "",
    window.location.pathname + window.location.search,
  );
  const basePath = gatewayBasePath();
  const key = capabilityKey(basePath);
  window.sessionStorage.removeItem(key);
  const response = await fetch(joinPath(basePath, "/launch"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
    credentials: "same-origin",
    redirect: "error",
  });
  if (!response.ok) {
    throw new Error("Open a new Heartwood launch link from your terminal.");
  }
  const payload: unknown = await response.json();
  if (
    typeof payload !== "object" ||
    payload === null ||
    !("capability" in payload) ||
    typeof payload.capability !== "string" ||
    payload.capability.length < 32 ||
    payload.capability.length > 4096
  ) {
    throw new Error("The gateway returned an invalid launch response.");
  }
  window.sessionStorage.setItem(key, payload.capability);
};

export const gatewayBasePath = (): string => {
  const env = import.meta.env as unknown;
  if (typeof env !== "object" || env === null) {
    return gatewayBaseFromDocument();
  }
  const value = (env as { VITE_HEARTWOOD_GATEWAY_BASE?: unknown })
    .VITE_HEARTWOOD_GATEWAY_BASE;
  if (typeof value === "string" && value !== "") {
    return value;
  }
  return gatewayBaseFromDocument();
};

const gatewayBaseFromDocument = (): string => {
  if (typeof document === "undefined") {
    return "";
  }
  const value = document
    .querySelector<HTMLMetaElement>('meta[name="heartwood-gateway-base"]')
    ?.content.trim();
  if (value === undefined || value === "" || value === "/") {
    return "";
  }
  return value.startsWith("/") && !value.endsWith("/") ? value : "";
};

export const joinPath = (basePath: string, path: string): string => {
  const base = basePath.endsWith("/") ? basePath.slice(0, -1) : basePath;
  return `${base}${path}`;
};
