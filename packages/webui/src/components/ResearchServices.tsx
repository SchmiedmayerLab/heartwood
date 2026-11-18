/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Badge } from "@schmiedmayerlab/grove-design-system/components/Badge";
import { Button } from "@schmiedmayerlab/grove-design-system/components/Button";
import { Checkbox } from "@schmiedmayerlab/grove-design-system/components/Checkbox";
import { Input } from "@schmiedmayerlab/grove-design-system/components/Input";
import {
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@schmiedmayerlab/grove-design-system/components/Sheet";
import { useEffect, useState } from "react";
import type { HeartwoodClient } from "../client";
import type { McpServer, McpServers, McpServerTest } from "../types";

type ServicesClient = Pick<
  HeartwoodClient,
  | "addMcpServer"
  | "disconnectMcpServer"
  | "getMcpServers"
  | "selectMcpTools"
  | "setMcpServerEnabled"
  | "testMcpServer"
>;

const toolStatusLabel: Record<McpServer["tools"][number]["status"], string> = {
  ready: "Reviewed",
  changed: "Changed since review",
  missing: "Not listed by the server",
  disallowed: "Not allowed by the deployment",
  unsupported: "No operation binding",
};

const authenticationLabel = ({ authentication }: McpServer) => {
  switch (authentication.status) {
    case "valid":
      return `Token valid until ${authentication.expires_at ?? "unknown"}`;
    case "expired":
      return "Token expired";
    case "unknown-expiry":
      return "Token without a readable expiry";
    case "missing":
      return "No token";
  }
};

const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Research services unavailable";

/** Manage research MCP servers through the gateway's shared operations. */
export const ResearchServices = ({ client }: { client: ServicesClient }) => {
  const [settings, setSettings] = useState<McpServers | null>(null);
  const [test, setTest] = useState<McpServerTest | null>(null);
  const [selection, setSelection] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState({
    serverId: "",
    label: "",
    url: "",
    token: "",
    remember: false,
  });
  const [testToken, setTestToken] = useState("");

  useEffect(() => {
    let current = true;
    client
      .getMcpServers()
      .then((value) => {
        if (current) setSettings(value);
      })
      .catch((caught: unknown) => {
        if (current) setError(errorMessage(caught));
      });
    return () => {
      current = false;
    };
  }, [client]);

  const run = async <T,>(
    operation: () => Promise<T>,
    apply: (value: T) => void,
  ) => {
    setBusy(true);
    setError(null);
    try {
      apply(await operation());
    } catch (caught: unknown) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const testServer = (serverId: string) =>
    run(
      () => client.testMcpServer(serverId, testToken.trim() || null, false),
      (result) => {
        setTestToken("");
        setTest(result);
        setSelection(
          result.tools
            .filter((tool) => tool.selected && !tool.changed)
            .map((tool) => tool.name),
        );
        void client.getMcpServers().then(setSettings);
      },
    );

  return (
    <>
      <SheetHeader>
        <SheetTitle>Research services</SheetTitle>
        <SheetDescription>
          MCP servers the gateway calls after you review each call
        </SheetDescription>
      </SheetHeader>
      {error ?
        <div className="connection-error" role="alert">
          {error}
        </div>
      : null}
      {settings?.availability === "unavailable" ?
        <p className="panel-empty" role="status">
          {settings.unavailable_code}: {settings.unavailable_reason}
        </p>
      : null}
      <section className="panel-section skill-list">
        <h3>Servers</h3>
        {settings?.servers.length ?
          settings.servers.map((server) => (
            <article className="skill-row" key={server.server_id}>
              <div>
                <strong>
                  {server.label}{" "}
                  <Badge variant={server.enabled ? "default" : "secondary"}>
                    {server.enabled ? "Enabled" : "Disabled"}
                  </Badge>
                </strong>
                <span>{server.url}</span>
                <small>
                  {server.destination === "allowed" ?
                    `Approved destination for ${server.service_id ?? "this service"}`
                  : "Destination not approved by the deployment"}
                </small>
                <small>{authenticationLabel(server)}</small>
                {server.tools.map((tool) => (
                  <small key={tool.name} title={tool.sha256}>
                    {tool.name}: {toolStatusLabel[tool.status]}
                  </small>
                ))}
                <small>
                  Enabled for new sessions:{" "}
                  {server.enabled_tools.length ?
                    server.enabled_tools.join(", ")
                  : "none"}
                </small>
              </div>
              <div className="service-actions">
                <Button
                  disabled={busy}
                  size="sm"
                  variant="outline"
                  onClick={() => void testServer(server.server_id)}
                >
                  Test
                </Button>
                <Button
                  disabled={busy}
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    void run(
                      () =>
                        client.setMcpServerEnabled(
                          server.server_id,
                          !server.enabled,
                        ),
                      setSettings,
                    )
                  }
                >
                  {server.enabled ? "Disable" : "Enable"}
                </Button>
                <Button
                  disabled={busy}
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    void run(
                      () => client.disconnectMcpServer(server.server_id),
                      (value) => {
                        setSettings(value);
                        if (test?.server.server_id === server.server_id)
                          setTest(null);
                      },
                    )
                  }
                >
                  Disconnect
                </Button>
              </div>
            </article>
          ))
        : <p className="panel-empty">No research services configured</p>}

        <small>
          Disconnecting removes the server and its token. Completed remote work
          is not undone.
        </small>
      </section>

      <details className="advanced-section">
        <summary>Test with a new token</summary>
        <div className="advanced-section-content">
          <label>
            Token for this server's service
            <Input
              autoComplete="off"
              type="password"
              value={testToken}
              onChange={(event) => setTestToken(event.target.value)}
            />
          </label>
          <small>
            The gateway keeps the token for every server of the same service and
            never shows it again.
          </small>
        </div>
      </details>
      {test ?
        <section className="panel-section skill-review" aria-live="polite">
          <h3>Review tools from {test.server.label}</h3>
          <span>
            Listing a tool does not authorize it. Hints come from the server.
          </span>
          {test.tools.map((tool) => {
            const selectable = tool.allowed && tool.accepts_operation_binding;
            return (
              <div className="service-tool" key={tool.name}>
                <label className="checkbox-control">
                  <Checkbox
                    aria-label={`Select ${tool.name}`}
                    checked={selection.includes(tool.name)}
                    disabled={!selectable || busy}
                    onCheckedChange={(checked) =>
                      setSelection((current) =>
                        checked === true ?
                          [...current, tool.name]
                        : current.filter((name) => name !== tool.name),
                      )
                    }
                  />
                  <span>
                    <strong>{tool.name}</strong> {tool.description}
                  </span>
                </label>
                <small>
                  {tool.allowed ? "" : "Not allowed by the deployment. "}
                  {tool.accepts_operation_binding ?
                    ""
                  : "Does not accept a project and operation identifier. "}
                  {tool.changed ? "Changed since your review. " : ""}
                  Read-only hint:{" "}
                  {tool.annotations.read_only === null ?
                    "not given"
                  : String(tool.annotations.read_only)}
                  .
                </small>
                <details>
                  <summary>Parameters the agent fills in</summary>
                  <pre>
                    {JSON.stringify(JSON.parse(tool.parameters), null, 2)}
                  </pre>
                  <small>Definition SHA-256 {tool.sha256}</small>
                </details>
              </div>
            );
          })}
          {test.resources.advertised || test.prompts.advertised ?
            <small>
              The server offers{" "}
              {[
                test.resources.advertised ? "resources" : null,
                test.prompts.advertised ? "prompts" : null,
              ]
                .filter(Boolean)
                .join(" and ")}
              ; Heartwood does not use them.
            </small>
          : null}
          <Button
            disabled={busy}
            onClick={() =>
              void run(
                () =>
                  client.selectMcpTools(
                    test.server.server_id,
                    test.tools
                      .filter((tool) => selection.includes(tool.name))
                      .map((tool) => ({
                        name: tool.name,
                        sha256: tool.sha256,
                      })),
                  ),
                (value) => {
                  setSettings(value);
                  setTest(null);
                },
              )
            }
          >
            Save reviewed selection
          </Button>
        </section>
      : null}

      <details className="advanced-section">
        <summary>Add a server</summary>
        <div className="advanced-section-content">
          <label>
            Identifier
            <Input
              value={draft.serverId}
              onChange={(event) =>
                setDraft({ ...draft, serverId: event.target.value })
              }
            />
          </label>
          <label>
            Name
            <Input
              value={draft.label}
              onChange={(event) =>
                setDraft({ ...draft, label: event.target.value })
              }
            />
          </label>
          <label>
            Server URL
            <Input
              value={draft.url}
              onChange={(event) =>
                setDraft({ ...draft, url: event.target.value })
              }
            />
          </label>
          <label>
            Token (optional)
            <Input
              autoComplete="off"
              type="password"
              value={draft.token}
              onChange={(event) =>
                setDraft({ ...draft, token: event.target.value })
              }
            />
          </label>
          <label className="checkbox-control">
            <Checkbox
              aria-label="Remember the token"
              checked={draft.remember}
              disabled={
                settings?.credential_store.persistence_available !== true
              }
              onCheckedChange={(checked) =>
                setDraft({ ...draft, remember: checked === true })
              }
            />
            Remember the token in{" "}
            {settings?.credential_store.persistence_description ??
              "the credential store"}
          </label>
          <Button
            disabled={
              busy ||
              !draft.serverId.trim() ||
              !draft.label.trim() ||
              !draft.url.trim()
            }
            variant="outline"
            onClick={() =>
              void run(
                () =>
                  client.addMcpServer({
                    server_id: draft.serverId.trim(),
                    label: draft.label.trim(),
                    url: draft.url.trim(),
                    token: draft.token.trim() || null,
                    remember: draft.remember,
                  }),
                (value) => {
                  setSettings(value);
                  setDraft({
                    serverId: "",
                    label: "",
                    url: "",
                    token: "",
                    remember: false,
                  });
                },
              )
            }
          >
            Add
          </Button>
          <small>
            Approved origins:{" "}
            {settings?.destinations.length ?
              settings.destinations.map((item) => item.origin).join(", ")
            : "none"}
            . A new server stays disabled until you test it, review its tools
            and enable it.
          </small>
        </div>
      </details>
    </>
  );
};
