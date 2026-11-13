/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  authenticatedFetch,
  gatewayCapability,
  initializeGatewayAccess,
} from "./gatewayAccess";

const capability = "c".repeat(43);
const key = "heartwood.gateway-capability:/proxy/8767/";

const configureGateway = (): void => {
  const meta = document.createElement("meta");
  meta.name = "heartwood-gateway-base";
  meta.content = "/proxy/8767";
  document.head.append(meta);
};

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
  window.history.replaceState({}, "", "/");
  document
    .querySelectorAll('meta[name="heartwood-gateway-base"]')
    .forEach((node) => node.remove());
});

describe("gateway browser capability", () => {
  it("removes the fragment before exchanging and preserves access across reloads", async () => {
    configureGateway();
    window.history.replaceState({}, "", "/proxy/8767/#launch=one-time-token");
    const fetch = vi.fn().mockImplementation(() => {
      expect(window.location.hash).toBe("");
      return Promise.resolve(new Response(JSON.stringify({ capability })));
    });
    vi.stubGlobal("fetch", fetch);
    await initializeGatewayAccess();
    expect(fetch).toHaveBeenCalledWith(
      "/proxy/8767/launch",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ token: "one-time-token" }),
        redirect: "error",
      }),
    );
    expect(gatewayCapability("/proxy/8767")).toBe(capability);
    await initializeGatewayAccess();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(document.cookie).not.toContain(capability);
  });

  it("adds a header only for the admitted origin and project path", async () => {
    window.sessionStorage.setItem(key, capability);
    const fetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    await authenticatedFetch("/proxy/8767", "/proxy/8767/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const options = fetch.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(options.headers).get("X-Heartwood-Capability")).toBe(
      capability,
    );
    expect(new Headers(options.headers).get("Content-Type")).toBe(
      "application/json",
    );
    expect(options.redirect).toBe("error");
    expect(options.credentials).toBe("same-origin");
    expect(gatewayCapability("/other-project")).toBeNull();
  });

  it.each([
    "http://127.0.0.1:9999/sessions",
    "https://outside.example/sessions",
  ])("rejects a different origin before dispatch: %s", (url) => {
    window.sessionStorage.setItem(key, capability);
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    expect(() => authenticatedFetch("/proxy/8767", url)).toThrow("origin");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects another project's path before forwarding the capability", () => {
    window.sessionStorage.setItem(key, capability);
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    expect(() =>
      authenticatedFetch("/proxy/8767", "/other-project/sessions"),
    ).toThrow("base path");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("supports a trusted proxy that injects its own credential", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    await initializeGatewayAccess();
    await authenticatedFetch("/proxy/8767", "/proxy/8767/sessions");
    const options = fetch.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(options.headers).has("X-Heartwood-Capability")).toBe(
      false,
    );
  });

  it.each([
    new Response("{}", { status: 403 }),
    new Response('{"capability":3}'),
  ])(
    "clears stale access and refuses an unsuccessful exchange",
    async (response) => {
      configureGateway();
      window.sessionStorage.setItem(key, capability);
      window.history.replaceState({}, "", "/proxy/8767/#launch=invalid");
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
      await expect(initializeGatewayAccess()).rejects.toThrow();
      expect(gatewayCapability("/proxy/8767")).toBeNull();
      expect(window.location.hash).toBe("");
    },
  );
});
