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
  MessageSquareText,
  Send,
  Settings,
  TerminalSquare,
} from "lucide-react";
import type { RefObject } from "react";
import type { ApprovalControl, ConversationMessage } from "../types";

interface ConversationWorkspaceProps {
  conversation: ConversationMessage[];
  conversationEndRef: RefObject<HTMLDivElement | null>;
  modelConfigured: boolean;
  paused: boolean;
  pendingActions: ApprovalControl[];
  prompt: string;
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
  paused,
  pendingActions,
  prompt,
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
        <span>No model profile is selected.</span>
        <Button size="sm" variant="outline" onClick={onOpenSettings}>
          <Settings size={15} />
          Configure model
        </Button>
      </div>
    : null}

    <div
      aria-label="Conversation transcript"
      className="conversation-list"
      role="log"
    >
      {conversation.length === 0 ?
        <EmptyConversation onPrompt={onPrompt} />
      : conversation.map((message) => (
          <ConversationItem key={message.id} message={message} />
        ))
      }
      <div ref={conversationEndRef} aria-hidden="true" />
    </div>

    <div className="composer-area">
      {pendingActions.map((control) => (
        <ApprovalRequest
          control={control}
          key={control.targetId}
          onDecision={onDecision}
        />
      ))}
      <div className="composer">
        <Textarea
          aria-label="Task"
          disabled={paused}
          placeholder={
            paused ?
              "Resume the session to continue"
            : "Ask Heartwood to work in this workspace"
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
              disabled={!prompt.trim() || paused}
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

const EmptyConversation = ({
  onPrompt,
}: {
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
  control,
  onDecision,
}: {
  control: ApprovalControl;
  onDecision: (decision: "approve" | "deny", control: ApprovalControl) => void;
}) => (
  <section
    className="approval-request"
    aria-label={`Approval required for ${control.toolName}`}
  >
    <div className="approval-copy">
      <div className="approval-heading">
        <small>Approval required</small>
        {control.risk ?
          <Badge variant="secondary">{control.risk} risk</Badge>
        : null}
      </div>
      <strong>{control.toolName || "Tool action"}</strong>
      {control.summary ?
        <p>{control.summary}</p>
      : null}
    </div>
    <div className="approval-actions">
      <Button
        aria-label={`Allow ${control.targetId}`}
        size="sm"
        onClick={() => onDecision("approve", control)}
      >
        <Check size={16} />
        Allow once
      </Button>
      <Button
        aria-label={`Reject ${control.targetId}`}
        size="sm"
        variant="outline"
        onClick={() => onDecision("deny", control)}
      >
        <Ban size={16} />
        Reject
      </Button>
    </div>
  </section>
);

const TASK_STARTERS = [
  "Inspect the workspace",
  "Summarize the available files",
  "Identify the next safe step",
];
