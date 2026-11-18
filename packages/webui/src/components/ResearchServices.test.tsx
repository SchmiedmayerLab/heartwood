/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import {
  Sheet,
  SheetContent,
} from "@schmiedmayerlab/grove-design-system/components/Sheet";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { HeartwoodClient } from "../client";
import type { McpServer, McpServers, McpServerTest } from "../types";
import { ResearchServices } from "./ResearchServices";

const server = (overrides: Partial<McpServer> = {}): McpServer => ({
  server_id: "catalog",
  label: "Synthetic catalog",
  url: "https://catalog.example.org/mcp",
  origin: "https://catalog.example.org",
  service_id: "synthetic-catalog",
  destination: "allowed",
  enabled: false,
  authentication: { status: "valid", expires_at: "2026-12-01T00:00:00Z" },
  tools: [],
  enabled_tools: [],
  ...overrides,
});

const settings = (servers: McpServer[] = [server()]): McpServers => ({
  availability: "available",
  unavailable_reason: null,
  unavailable_code: null,
  destinations: [
    {
      service_id: "synthetic-catalog",
      origin: "https://catalog.example.org",
      tools: ["find_records"],
      max_result_bytes: 16384,
      credential_binding: "HEARTWOOD_MCP_TOKEN_SYNTHETIC_CATALOG",
      private_address: false,
    },
  ],
  credential_store: {
    backends: ["process"],
    default_backend: "process",
    persistence_available: false,
    persistence_description: "Current Heartwood process only",
  },
  servers,
});

const hints = {
  read_only: true,
  destructive: false,
  idempotent: true,
  open_world: false,
};

const tested = (): McpServerTest => ({
  server: server(),
  tools: [
    {
      name: "find_records",
      description: "Find synthetic records.",
      sha256: "a".repeat(64),
      parameters:
        '{"properties":{"term":{"type":"string"}},"required":["term"],"type":"object"}',
      definition:
        '{"name":"find_records","description":"Find synthetic records."}',
      annotations: hints,
      allowed: true,
      accepts_operation_binding: true,
      selected: false,
      changed: false,
    },
    {
      name: "delete_records",
      description: "Delete synthetic records.",
      sha256: "b".repeat(64),
      parameters: '{"properties":{},"required":[],"type":"object"}',
      definition: '{"name":"delete_records"}',
      annotations: { ...hints, read_only: false },
      allowed: false,
      accepts_operation_binding: true,
      selected: false,
      changed: false,
    },
  ],
  resources: { advertised: true, support: "unsupported" },
  prompts: { advertised: false, support: "unsupported" },
});

const client = () => ({
  getMcpServers: vi
    .fn<HeartwoodClient["getMcpServers"]>()
    .mockResolvedValue(settings()),
  addMcpServer: vi
    .fn<HeartwoodClient["addMcpServer"]>()
    .mockResolvedValue(settings()),
  testMcpServer: vi
    .fn<HeartwoodClient["testMcpServer"]>()
    .mockResolvedValue(tested()),
  selectMcpTools: vi.fn<HeartwoodClient["selectMcpTools"]>().mockResolvedValue(
    settings([
      server({
        enabled: true,
        tools: [
          {
            name: "find_records",
            worker_tool: "catalog__find_records_aaaaaaaa",
            description: "Find synthetic records.",
            sha256: "a".repeat(64),
            parameters: "{}",
            definition: '{"name":"find_records"}',
            annotations: hints,
            status: "ready",
          },
        ],
        enabled_tools: ["catalog__find_records_aaaaaaaa"],
      }),
    ]),
  ),
  setMcpServerEnabled: vi
    .fn<HeartwoodClient["setMcpServerEnabled"]>()
    .mockResolvedValue(settings([server({ enabled: true })])),
  disconnectMcpServer: vi
    .fn<HeartwoodClient["disconnectMcpServer"]>()
    .mockResolvedValue(settings([])),
});

const renderPanel = (api: ReturnType<typeof client>) =>
  render(
    <Sheet open>
      <SheetContent>
        <ResearchServices client={api} />
      </SheetContent>
    </Sheet>,
  );

describe("ResearchServices", () => {
  it("shows each server's destination, credential state and enabled tools", async () => {
    renderPanel(client());
    expect(await screen.findByText("Synthetic catalog")).toBeInTheDocument();
    expect(
      screen.getByText("Approved destination for synthetic-catalog"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Token valid until 2026-12-01T00:00:00Z"),
    ).toBeInTheDocument();
    expect(screen.getByText("Enabled for new sessions: none")).toBeVisible();
  });

  it("reviews listed tools and saves only selectable ones", async () => {
    const api = client();
    renderPanel(api);
    fireEvent.click(await screen.findByRole("button", { name: "Test" }));
    expect(
      await screen.findByText(/Listing a tool does not authorize it/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Select delete_records")).toBeDisabled();
    expect(
      screen.getByText(/The server offers resources; Heartwood does not use/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Select find_records"));
    fireEvent.click(
      screen.getByRole("button", { name: "Save reviewed selection" }),
    );
    await waitFor(() =>
      expect(api.selectMcpTools).toHaveBeenCalledWith("catalog", [
        { name: "find_records", sha256: "a".repeat(64) },
      ]),
    );
    expect(
      await screen.findByText(
        "Enabled for new sessions: catalog__find_records_aaaaaaaa",
      ),
    ).toBeInTheDocument();
  });

  it("adds a disabled server and disconnects it", async () => {
    const api = client();
    renderPanel(api);
    await screen.findByText("Synthetic catalog");
    fireEvent.click(screen.getByText("Add a server"));
    fireEvent.change(screen.getByLabelText("Identifier"), {
      target: { value: "notes" },
    });
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Synthetic notes" },
    });
    fireEvent.change(screen.getByLabelText("Server URL"), {
      target: { value: "https://catalog.example.org/notes" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(api.addMcpServer).toHaveBeenCalledWith({
        server_id: "notes",
        label: "Synthetic notes",
        url: "https://catalog.example.org/notes",
        token: null,
        remember: false,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(
      await screen.findByText("No research services configured"),
    ).toBeInTheDocument();
  });

  it("states why research services are unavailable", async () => {
    const api = client();
    api.getMcpServers.mockResolvedValue({
      ...settings([]),
      availability: "unavailable",
      unavailable_code: "HW-MCP-004",
      unavailable_reason:
        "research services run only in isolated worker sessions",
    });
    renderPanel(api);
    expect(await screen.findByText(/HW-MCP-004/)).toBeInTheDocument();
  });
});
