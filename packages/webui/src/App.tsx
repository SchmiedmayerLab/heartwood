/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Badge } from "@stanfordspezi/spezi-web-design-system/components/Badge";
import { Button } from "@stanfordspezi/spezi-web-design-system/components/Button";
import {
  Card,
  CardHeader,
  CardTitle,
} from "@stanfordspezi/spezi-web-design-system/components/Card";
import { Input } from "@stanfordspezi/spezi-web-design-system/components/Input";
import { Textarea } from "@stanfordspezi/spezi-web-design-system/components/Textarea";
import { SpeziProvider } from "@stanfordspezi/spezi-web-design-system/SpeziProvider";
import {
  Activity,
  BrainCircuit,
  Check,
  Database,
  Download,
  MessageSquare,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  TerminalSquare,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { GatewayClient, createCommand, type HeartwoodClient } from "./client";
import type {
  ApprovalControl,
  ConversationMessage,
  JsonValue,
  SessionEvent,
} from "./types";
import { buildViewModel } from "./viewModel";

interface AppProps {
  client?: HeartwoodClient;
  initialSessionId?: string;
}

interface LocalConversationMessage extends ConversationMessage {
  sessionId: string;
}

export const App = ({
  client,
  initialSessionId = "session-local",
}: AppProps) => {
  const [sessionId, setSessionId] = useState(initialSessionId);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [prompt, setPrompt] = useState(
    "Summarize the synthetic cohort quality check in one sentence.",
  );
  const [endpoint, setEndpoint] = useState(
    "http://127.0.0.1:8765/v1/chat/completions",
  );
  const [providerConfigPath, setProviderConfigPath] = useState(
    "/opt/heartwood/images/generic/providers/provider-routes.example.toml",
  );
  const [providerRouteId, setProviderRouteId] = useState("local-loopback");
  const [invokeProvider, setInvokeProvider] = useState(true);
  const [localConversation, setLocalConversation] = useState<
    LocalConversationMessage[]
  >([]);
  const [status, setStatus] = useState<"idle" | "busy" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const conversationEndRef = useRef<HTMLDivElement | null>(null);
  const resolvedClient = useMemo(() => client ?? new GatewayClient(), [client]);
  const viewModel = useMemo(() => buildViewModel(events), [events]);
  const conversation = useMemo(
    () =>
      mergeConversationMessages(
        localConversation.filter((message) => message.sessionId === sessionId),
        viewModel.conversation,
      ),
    [localConversation, sessionId, viewModel.conversation],
  );
  const commandSequence = events.length;

  useEffect(() => {
    let active = true;
    resolvedClient
      .replayEvents(sessionId)
      .then(({ events: replayed }) => {
        if (active) {
          setEvents(replayed);
          setStatus("idle");
        }
      })
      .catch((caught: unknown) => {
        if (active) {
          setError(errorMessage(caught));
          setStatus("error");
        }
      });
    const cleanup = resolvedClient.streamEvents(
      sessionId,
      undefined,
      (streamed) => {
        setEvents((current) => mergeEvents(current, streamed));
      },
    );
    return () => {
      active = false;
      cleanup();
    };
  }, [resolvedClient, sessionId]);

  useEffect(() => {
    scrollConversationEnd(conversationEndRef.current);
  }, [conversation.length]);

  const send = async (
    kind: Parameters<typeof createCommand>[1],
    payload: Record<string, JsonValue> = {},
  ) => {
    setStatus("busy");
    setError(null);
    try {
      const command = createCommand(sessionId, kind, commandSequence, payload);
      const submittedPrompt = promptContent(payload);
      if ((kind === "chat" || kind === "run") && submittedPrompt) {
        setLocalConversation((current) => [
          ...current,
          {
            id: `local-${command.command_id}`,
            sequence: (events.at(-1)?.sequence ?? -1) + 0.5,
            role: "user",
            label: "You",
            content: submittedPrompt,
            detail: null,
            sessionId,
          },
        ]);
      }
      const response = await resolvedClient.postCommand(command);
      setEvents((current) => mergeEvents(current, response.events));
      setStatus("idle");
    } catch (caught) {
      setError(errorMessage(caught));
      setStatus("error");
    }
  };

  const decideApproval = (kind: "approve" | "deny", control: ApprovalControl) =>
    send(kind, {
      reason: "web UI demo decision",
      target_id: control.targetId,
      target_type: control.targetType,
    });

  const runPayload = (): Record<string, JsonValue> => {
    const payload: Record<string, JsonValue> = {
      endpoint,
      invoke_provider: invokeProvider,
      prompt,
    };
    if (providerRouteId) {
      payload.provider_route_id = providerRouteId;
    }
    if (providerConfigPath) {
      payload.provider_config_path = providerConfigPath;
    }
    return payload;
  };

  return (
    <SpeziProvider
      router={{
        Link: ({ href, ...props }) => <a href={href ?? "#"} {...props} />,
      }}
    >
      <main className="app-shell">
        <header className="topbar">
          <div>
            <h1>Heartwood</h1>
            <div className="session-row">
              <Input
                aria-label="Session ID"
                value={sessionId}
                onChange={(event) => setSessionId(event.target.value)}
              />
              <Badge
                variant={status === "error" ? "destructiveLight" : "secondary"}
              >
                {status}
              </Badge>
            </div>
          </div>
          <div className="topbar-actions">
            <Button
              aria-label="Detect"
              size="sm"
              variant="outline"
              onClick={() => void send("detect")}
            >
              <Database size={16} />
              Detect
            </Button>
            <Button
              aria-label="Replay"
              size="sm"
              variant="outline"
              onClick={() =>
                void resolvedClient
                  .replayEvents(sessionId)
                  .then(({ events: replayed }) => {
                    setEvents(replayed);
                  })
              }
            >
              <RotateCcw size={16} />
              Replay
            </Button>
            <Button
              aria-label="Export Audit"
              size="sm"
              variant="outline"
              onClick={() => void send("audit.export")}
            >
              <Download size={16} />
              Audit
            </Button>
          </div>
        </header>

        {error ?
          <div className="error-banner">{error}</div>
        : null}

        <section className="workspace-grid">
          <Card className="panel command-panel">
            <CardHeader>
              <CardTitle asChild>
                <h2>
                  <TerminalSquare size={18} />
                  Run
                </h2>
              </CardTitle>
            </CardHeader>
            <div className="panel-body">
              <label>
                Prompt
                <Textarea
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                />
              </label>
              <label>
                Endpoint
                <Input
                  value={endpoint}
                  onChange={(event) => setEndpoint(event.target.value)}
                />
              </label>
              <div className="provider-grid">
                <label>
                  Provider Config
                  <Input
                    value={providerConfigPath}
                    onChange={(event) =>
                      setProviderConfigPath(event.target.value)
                    }
                  />
                </label>
                <label>
                  Route
                  <Input
                    value={providerRouteId}
                    onChange={(event) => setProviderRouteId(event.target.value)}
                  />
                </label>
              </div>
              <label className="checkbox-row">
                <input
                  checked={invokeProvider}
                  type="checkbox"
                  onChange={(event) => setInvokeProvider(event.target.checked)}
                />
                Invoke Provider
              </label>
              <div className="button-row">
                <Button
                  isPending={status === "busy"}
                  onClick={() => void send("run", runPayload())}
                >
                  <Play size={16} />
                  {invokeProvider ? "Run Local Model" : "Run"}
                </Button>
                <Button variant="outline" onClick={() => void send("pause")}>
                  <Pause size={16} />
                  Pause
                </Button>
                <Button variant="outline" onClick={() => void send("resume")}>
                  <RefreshCw size={16} />
                  Resume
                </Button>
              </div>
            </div>
          </Card>

          <Card className="panel conversation-panel">
            <CardHeader>
              <CardTitle asChild>
                <h2>
                  <MessageSquare size={18} />
                  Conversation
                </h2>
              </CardTitle>
            </CardHeader>
            <div
              aria-label="Conversation transcript"
              className="panel-body conversation-list"
              role="log"
            >
              {conversation.length === 0 ?
                <div className="empty-state">no conversation yet</div>
              : conversation.map((message) => (
                  <article
                    className={`conversation-message ${message.role}`}
                    key={message.id}
                  >
                    <div className="conversation-meta">
                      <small>{message.label}</small>
                      {message.detail ?
                        <span>{message.detail}</span>
                      : null}
                    </div>
                    <p>{message.content}</p>
                  </article>
                ))
              }
              <div ref={conversationEndRef} aria-hidden="true" />
            </div>
          </Card>

          <StatusPanels
            events={events}
            isBusy={status === "busy"}
            onApprovalDecision={decideApproval}
          />
        </section>
      </main>
    </SpeziProvider>
  );
};

const promptContent = (payload: Record<string, JsonValue>): string => {
  const prompt = payload.prompt;
  return typeof prompt === "string" ? prompt.trim() : "";
};

const mergeConversationMessages = (
  localMessages: ConversationMessage[],
  eventMessages: ConversationMessage[],
): ConversationMessage[] =>
  [...localMessages, ...eventMessages].sort(
    (left, right) =>
      left.sequence - right.sequence || left.id.localeCompare(right.id),
  );

const scrollConversationEnd = (target: unknown): void => {
  if (hasScrollIntoView(target)) {
    target.scrollIntoView({ block: "end" });
  }
};

const hasScrollIntoView = (
  value: unknown,
): value is { scrollIntoView: (options?: ScrollIntoViewOptions) => void } => {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as { scrollIntoView?: unknown };
  return typeof candidate.scrollIntoView === "function";
};

const StatusPanels = ({
  events,
  isBusy,
  onApprovalDecision,
}: {
  events: SessionEvent[];
  isBusy: boolean;
  onApprovalDecision: (
    kind: "approve" | "deny",
    control: ApprovalControl,
  ) => Promise<void>;
}) => {
  const viewModel = buildViewModel(events);
  return (
    <>
      <Card className="panel">
        <CardHeader>
          <CardTitle asChild>
            <h2>
              <Database size={18} />
              Datasets
            </h2>
          </CardTitle>
        </CardHeader>
        <div className="panel-body list">
          {viewModel.datasetProposals.map((proposal) => (
            <div className="list-row" key={proposal.sourceId}>
              <span>{proposal.datasetType}</span>
              <Badge variant="outline">{proposal.confidence.toFixed(2)}</Badge>
            </div>
          ))}
        </div>
      </Card>

      <Card className="panel model-panel">
        <CardHeader>
          <CardTitle asChild>
            <h2>
              <BrainCircuit size={18} />
              Local Model
            </h2>
          </CardTitle>
        </CardHeader>
        <div className="panel-body list">
          {viewModel.modelInvocations.length === 0 ?
            <div className="empty-state">pending</div>
          : viewModel.modelInvocations.map((invocation, index) => (
              <div
                className="list-row stacked"
                key={`${invocation.routeId ?? invocation.model ?? "model"}-${index}`}
              >
                <span>
                  {invocation.status}
                  {invocation.routeId ? ` via ${invocation.routeId}` : ""}
                </span>
                {invocation.responsePreview ?
                  <p className="model-preview">{invocation.responsePreview}</p>
                : null}
                <small>
                  {[
                    invocation.provider,
                    invocation.model,
                    invocation.choicesCount === null ?
                      null
                    : `${invocation.choicesCount} choices`,
                    invocation.totalTokens === null ?
                      null
                    : `${invocation.totalTokens} tokens`,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </small>
              </div>
            ))
          }
        </div>
      </Card>

      <Card className="panel">
        <CardHeader>
          <CardTitle asChild>
            <h2>
              <ShieldCheck size={18} />
              Policy
            </h2>
          </CardTitle>
        </CardHeader>
        <div className="panel-body list">
          {viewModel.policyStatus.map((status, index) => (
            <div
              className="list-row stacked"
              key={`${status.endpoint}-${index}`}
            >
              <span>
                {status.decision}{" "}
                {status.routeId ? `via ${status.routeId}` : ""}
              </span>
              <small>{status.endpoint}</small>
            </div>
          ))}
        </div>
      </Card>

      <Card className="panel">
        <CardHeader>
          <CardTitle asChild>
            <h2>
              <Check size={18} />
              Approvals
            </h2>
          </CardTitle>
        </CardHeader>
        <div className="panel-body list">
          {viewModel.approvalControls.map((control) => (
            <div
              className="list-row approval-row"
              key={`${control.targetType}-${control.targetId}-${control.decision}`}
            >
              <span>
                {control.label}
                <small>{control.targetId}</small>
              </span>
              <Badge
                variant={
                  control.decision === "denied" ?
                    "destructiveLight"
                  : "secondary"
                }
              >
                {control.decision ?? control.targetType}
              </Badge>
              {control.decision === null ?
                <div className="approval-actions">
                  <Button
                    aria-label={`Approve ${control.targetId}`}
                    disabled={isBusy}
                    size="sm"
                    variant="outline"
                    onClick={() => void onApprovalDecision("approve", control)}
                  >
                    <Check size={14} />
                    Approve
                  </Button>
                  <Button
                    aria-label={`Deny ${control.targetId}`}
                    disabled={isBusy}
                    size="sm"
                    variant="outline"
                    onClick={() => void onApprovalDecision("deny", control)}
                  >
                    <X size={14} />
                    Deny
                  </Button>
                </div>
              : null}
            </div>
          ))}
        </div>
      </Card>

      <Card className="panel activity-panel">
        <CardHeader>
          <CardTitle asChild>
            <h2>
              <Activity size={18} />
              Activity
            </h2>
          </CardTitle>
        </CardHeader>
        <div className="panel-body activity-list">
          {viewModel.activity.map((item) => (
            <div className="activity-row" key={`${item.sequence}-${item.kind}`}>
              <span>{String(item.sequence).padStart(3, "0")}</span>
              <strong>{item.label}</strong>
              <small>{item.detail}</small>
            </div>
          ))}
        </div>
      </Card>

      <Card className="panel">
        <CardHeader>
          <CardTitle asChild>
            <h2>
              <X size={18} />
              Exports
            </h2>
          </CardTitle>
        </CardHeader>
        <div className="panel-body list">
          {viewModel.exportActions.map((action) => (
            <div className="list-row stacked" key={action.path}>
              <span>{action.label}</span>
              <small>{action.path}</small>
            </div>
          ))}
        </div>
      </Card>
    </>
  );
};

const mergeEvents = (
  current: SessionEvent[],
  next: SessionEvent[],
): SessionEvent[] => {
  const byId = new Map(current.map((event) => [event.event_id, event]));
  for (const event of next) {
    byId.set(event.event_id, event);
  }
  return [...byId.values()].sort(
    (left, right) => left.sequence - right.sequence,
  );
};

const errorMessage = (value: unknown): string => {
  if (value instanceof Error) {
    return value.message;
  }
  return String(value);
};
