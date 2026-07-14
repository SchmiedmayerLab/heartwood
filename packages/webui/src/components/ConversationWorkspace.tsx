/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Badge } from "@stanfordspezi/spezi-web-design-system/components/Badge";
import { Button } from "@stanfordspezi/spezi-web-design-system/components/Button";
import { Textarea } from "@stanfordspezi/spezi-web-design-system/components/Textarea";
import { Tooltip } from "@stanfordspezi/spezi-web-design-system/components/Tooltip";
import {
  Ban,
  Check,
  CirclePause,
  CirclePlay,
  LoaderCircle,
  MessageSquareText,
  Send,
  Settings,
  TerminalSquare,
} from "lucide-react";
import { useEffect, useState, type RefObject } from "react";
import type { RequestActivity as RequestActivityState } from "../requestActivity";
import type { ApprovalControl, ConversationMessage } from "../types";

interface ConversationWorkspaceProps {
  conversation: ConversationMessage[];
  conversationEndRef: RefObject<HTMLDivElement | null>;
  modelConfigured: boolean;
  modelMessage: string;
  paused: boolean;
  pendingActions: ApprovalControl[];
  prompt: string;
  requestActivity: RequestActivityState | null;
  requestStatus: "idle" | "busy" | "error";
  onDecision: (decision: "approve" | "deny", control: ApprovalControl) => void;
  onOpenSettings: () => void;
  onPauseToggle: () => void;
  onPrompt: (prompt: string) => void;
  onSubmit: () => void;
}

export const ConversationWorkspace = ({
  conversation,
  conversationEndRef,
  modelConfigured,
  modelMessage,
  paused,
  pendingActions,
  prompt,
  requestActivity,
  requestStatus,
  onDecision,
  onOpenSettings,
  onPauseToggle,
  onPrompt,
  onSubmit,
}: ConversationWorkspaceProps) => (
  <section className="conversation-workspace" aria-label="Agent conversation">
    {!modelConfigured ?
      <div className="configuration-banner" role="status">
        <span>{modelMessage}</span>
        <Button size="sm" variant="outline" onClick={onOpenSettings}>
          <Settings size={15} />
          Open settings
        </Button>
      </div>
    : null}

    <div
      aria-label="Conversation transcript"
      className="conversation-list"
      role="log"
    >
      {conversation.length === 0 ?
        <EmptyConversation disabled={!modelConfigured} onPrompt={onPrompt} />
      : conversation.map((message) => (
          <ConversationItem key={message.id} message={message} />
        ))
      }
      {requestStatus === "busy" && requestActivity !== null ?
        <RequestActivity activity={requestActivity} />
      : null}
      <div ref={conversationEndRef} aria-hidden="true" />
    </div>

    <div className="composer-area">
      {pendingActions.length > 0 ?
        <ApprovalRequest
          busy={requestStatus === "busy"}
          controls={pendingActions}
          onDecision={onDecision}
        />
      : null}
      <div className="composer">
        <Textarea
          aria-label="Task"
          disabled={
            paused ||
            !modelConfigured ||
            pendingActions.length > 0 ||
            requestStatus === "busy"
          }
          placeholder={
            paused ? "Resume the session to continue"
            : !modelConfigured ?
              "Choose an authorized model to start"
            : requestStatus === "busy" ?
              "Heartwood is working on the current request"
            : "Ask Heartwood to work in this project"
          }
          value={prompt}
          onChange={(event) => onPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSubmit();
            }
          }}
        />
        <div className="composer-actions">
          <Tooltip tooltip={paused ? "Resume agent" : "Pause agent"}>
            <Button
              aria-label={paused ? "Resume agent" : "Pause agent"}
              disabled={!modelConfigured || requestStatus === "busy"}
              size="sm"
              variant="ghost"
              onClick={onPauseToggle}
            >
              {paused ?
                <CirclePlay size={18} />
              : <CirclePause size={18} />}
            </Button>
          </Tooltip>
          <Tooltip tooltip="Send task">
            <Button
              aria-label="Send task"
              disabled={
                !prompt.trim() ||
                paused ||
                !modelConfigured ||
                pendingActions.length > 0 ||
                requestStatus === "busy"
              }
              isPending={requestStatus === "busy"}
              size="sm"
              onClick={onSubmit}
            >
              <Send size={17} />
            </Button>
          </Tooltip>
        </div>
      </div>
    </div>
  </section>
);

const RequestActivity = ({ activity }: { activity: RequestActivityState }) => {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, []);
  const waiting = elapsed >= 10;
  const label = waiting ? activity.waitingLabel : activity.label;
  return (
    <div
      aria-atomic="true"
      aria-label={waiting ? `${label}. ${activity.guidance}` : label}
      aria-live="polite"
      className="request-activity"
      role="status"
    >
      <LoaderCircle
        aria-hidden="true"
        className="request-activity-icon"
        size={17}
      />
      <div>
        <strong>{label}</strong>
        {waiting ?
          <span>{activity.guidance}</span>
        : null}
      </div>
      {waiting ?
        <small aria-hidden="true">{elapsed}s elapsed</small>
      : null}
    </div>
  );
};

const EmptyConversation = ({
  disabled,
  onPrompt,
}: {
  disabled: boolean;
  onPrompt: (prompt: string) => void;
}) => (
  <div className="conversation-empty">
    <span className="empty-icon" aria-hidden="true">
      <MessageSquareText size={22} />
    </span>
    <h2>Start an analysis</h2>
    <div className="starter-actions" aria-label="Task starters">
      {TASK_STARTERS.map((starter) => (
        <Button
          disabled={disabled}
          key={starter}
          size="sm"
          variant="outline"
          onClick={() => onPrompt(starter)}
        >
          {starter}
        </Button>
      ))}
    </div>
  </div>
);

const ConversationItem = ({ message }: { message: ConversationMessage }) => {
  if (message.role === "trace") {
    return (
      <div className="trace-message">
        <TerminalSquare size={15} aria-hidden="true" />
        <div>
          <strong>{message.content}</strong>
          {message.detail ?
            <span>{message.detail}</span>
          : null}
        </div>
      </div>
    );
  }
  return (
    <article className={`conversation-message ${message.role}`}>
      <div className="conversation-meta">
        <small>{message.label}</small>
        {message.detail ?
          <span>{message.detail}</span>
        : null}
      </div>
      <p>{message.content}</p>
    </article>
  );
};

const ApprovalRequest = ({
  busy,
  controls,
  onDecision,
}: {
  busy: boolean;
  controls: ApprovalControl[];
  onDecision: (decision: "approve" | "deny", control: ApprovalControl) => void;
}) => {
  // The gateway resolves the complete OpenHands action set from any member identifier.
  const setRepresentative = controls[0];
  if (!setRepresentative) return null;
  const label = controls.length === 1 ? "action" : "actions";
  return (
    <section
      className="approval-request"
      aria-label="Approval required for OpenHands action set"
      aria-busy={busy}
    >
      <div className="approval-copy">
        <div className="approval-heading">
          <small>Approval required</small>
          <Badge variant="secondary">
            {controls.length} {label}
          </Badge>
        </div>
        <strong>Review the complete action set</strong>
        <p>
          OpenHands proposed these actions together. One decision applies to
          every action below.
        </p>
        <ol className="approval-batch-list">
          {controls.map((control) => (
            <li key={control.targetId}>
              <span>{control.summary ?? control.toolName}</span>
              <small>
                {control.toolName || "tool"}
                {control.risk ? ` · ${control.risk} risk` : ""}
              </small>
            </li>
          ))}
        </ol>
      </div>
      <div className="approval-actions">
        <Button
          aria-label={`Allow all ${controls.length} ${label} once`}
          disabled={busy}
          size="sm"
          onClick={() => onDecision("approve", setRepresentative)}
        >
          <Check size={16} />
          Allow all once
        </Button>
        <Button
          aria-label={`Reject all ${controls.length} ${label}`}
          disabled={busy}
          size="sm"
          variant="outline"
          onClick={() => onDecision("deny", setRepresentative)}
        >
          <Ban size={16} />
          Reject all
        </Button>
      </div>
    </section>
  );
};

const TASK_STARTERS = [
  "Inspect the project",
  "Summarize the available files",
  "Identify the next safe step",
];
