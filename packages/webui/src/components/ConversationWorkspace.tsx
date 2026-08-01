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
  CircleCheck,
  CirclePause,
  CirclePlay,
  CircleX,
  Clock3,
  ListChecks,
  LoaderCircle,
  MessageSquareText,
  Send,
  Settings,
  ShieldAlert,
  TerminalSquare,
} from "lucide-react";
import { useEffect, useRef, useState, type RefObject } from "react";
import {
  actionCountLabel,
  actionRiskPresentation,
  actionStateLabel,
  actionToolLabel,
} from "../actionPresentation";
import type { RequestActivity as RequestActivityState } from "../requestActivity";
import type {
  ActionPresentation,
  ConversationMessage,
  ProjectionActionRecord,
  ProjectionApprovalGroup,
  ProjectionSuggestion,
  SessionProjection,
} from "../types";
import { displaySafeText, SafeMarkdown } from "./SafeMarkdown";

interface ConversationWorkspaceProps {
  conversationEndRef: RefObject<HTMLDivElement | null>;
  actionModeLabel: string | null;
  actionPresentation: ActionPresentation | null;
  modelConfigured: boolean;
  modelMessage: string;
  projection: SessionProjection | null;
  prompt: string;
  requestActivity: RequestActivityState | null;
  requestStatus: "idle" | "busy" | "error";
  onDecision: (
    decision: "approve" | "deny",
    approval: ProjectionApprovalGroup,
  ) => void;
  onOpenSettings: () => void;
  onPauseToggle: () => void;
  onPrompt: (prompt: string) => void;
  onSubmit: () => void;
}

export const ConversationWorkspace = ({
  conversationEndRef,
  actionModeLabel,
  actionPresentation,
  modelConfigured,
  modelMessage,
  projection,
  prompt,
  requestActivity,
  requestStatus,
  onDecision,
  onOpenSettings,
  onPauseToggle,
  onPrompt,
  onSubmit,
}: ConversationWorkspaceProps) => {
  const conversation = projection?.conversation ?? [];
  const pendingApproval =
    projection?.pendingApproval?.decision === null ?
      projection.pendingApproval
    : null;
  const paused = projection?.paused ?? false;
  const availableCommands = projection?.availableCommands ?? [];
  const canChat = availableCommands.includes("chat");
  const canPause = availableCommands.includes("pause");
  const canResume = availableCommands.includes("resume");
  const running = projection?.lifecycle.status === "running";
  const completedActions =
    projection?.actions.filter(
      (action) =>
        action.state !== "awaiting-review" && action.state !== "proposed",
    ) ?? [];
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const previousApprovalId = useRef<string | null>(
    pendingApproval?.groupId ?? null,
  );

  useEffect(() => {
    if (pendingApproval !== null) {
      previousApprovalId.current = pendingApproval.groupId;
      return;
    }
    if (
      previousApprovalId.current === null ||
      !modelConfigured ||
      !canChat ||
      requestStatus === "busy"
    )
      return;
    previousApprovalId.current = null;
    composerRef.current?.focus();
  }, [canChat, modelConfigured, pendingApproval, requestStatus]);

  const selectSuggestion = (suggestion: ProjectionSuggestion): void => {
    onPrompt(suggestion.prompt);
    window.requestAnimationFrame(() => composerRef.current?.focus());
  };

  return (
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
        tabIndex={0}
      >
        {conversation.length === 0 && !projection?.streamingText ?
          <EmptyConversation
            disabled={!modelConfigured || !canChat}
            suggestions={projection?.suggestions ?? []}
            onSelect={selectSuggestion}
          />
        : conversation.map((message) => (
            <ConversationItem key={message.id} message={message} />
          ))
        }
        <ActionHistory
          actions={completedActions}
          actionPresentation={actionPresentation}
        />
        {projection?.streamingText ?
          <StreamingMessage content={projection.streamingText} />
        : null}
        {requestStatus === "busy" && requestActivity !== null ?
          <RequestActivity activity={requestActivity} />
        : null}
        <RuntimeStatus projection={projection} />
        <div ref={conversationEndRef} aria-hidden="true" />
      </div>

      <div className="composer-area">
        {pendingApproval ?
          <ApprovalRequest
            actionModeLabel={actionModeLabel}
            actionPresentation={actionPresentation}
            approval={pendingApproval}
            canApprove={availableCommands.includes("approve")}
            canDeny={availableCommands.includes("deny")}
            busy={requestStatus === "busy"}
            onDecision={onDecision}
          />
        : (
          conversation.length > 0 &&
          projection !== null &&
          projection.suggestions.length > 0 &&
          !running
        ) ?
          <SuggestionActions
            compact
            disabled={!modelConfigured || !canChat}
            suggestions={projection.suggestions}
            onSelect={selectSuggestion}
          />
        : null}
        <div className="composer">
          <Textarea
            aria-label="Task"
            ref={composerRef}
            disabled={
              !modelConfigured ||
              !canChat ||
              pendingApproval !== null ||
              requestStatus === "busy"
            }
            placeholder={
              paused ? "Resume the session to continue"
              : !modelConfigured ?
                "Choose an authorized model to start"
              : running ?
                "Send guidance while Heartwood is working"
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
            <Tooltip tooltip={canResume ? "Resume agent" : "Pause agent"}>
              <Button
                aria-label={canResume ? "Resume agent" : "Pause agent"}
                disabled={
                  !modelConfigured ||
                  (!canPause && !canResume) ||
                  requestStatus === "busy"
                }
                size="sm"
                variant="ghost"
                onClick={onPauseToggle}
              >
                {canResume ?
                  <CirclePlay size={18} />
                : <CirclePause size={18} />}
              </Button>
            </Tooltip>
            <Tooltip tooltip={running ? "Send guidance" : "Send task"}>
              <Button
                aria-label={running ? "Send guidance" : "Send task"}
                disabled={
                  !prompt.trim() ||
                  !modelConfigured ||
                  !canChat ||
                  pendingApproval !== null ||
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
};

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
  suggestions,
  onSelect,
}: {
  disabled: boolean;
  suggestions: readonly ProjectionSuggestion[];
  onSelect: (suggestion: ProjectionSuggestion) => void;
}) => (
  <div className="conversation-empty">
    <span className="empty-icon" aria-hidden="true">
      <MessageSquareText size={22} />
    </span>
    <h2>Start an analysis</h2>
    <SuggestionActions
      disabled={disabled}
      suggestions={suggestions}
      onSelect={onSelect}
    />
  </div>
);

const SuggestionActions = ({
  compact = false,
  disabled,
  suggestions,
  onSelect,
}: {
  compact?: boolean;
  disabled: boolean;
  suggestions: readonly ProjectionSuggestion[];
  onSelect: (suggestion: ProjectionSuggestion) => void;
}) => {
  if (suggestions.length === 0) return null;
  return (
    <div
      aria-label="Suggested next steps"
      className={compact ? "suggestion-actions compact" : "suggestion-actions"}
    >
      {suggestions.map((suggestion) => (
        <Button
          disabled={disabled}
          key={suggestion.suggestionId}
          size="sm"
          variant="outline"
          onClick={() => onSelect(suggestion)}
        >
          {suggestion.label}
        </Button>
      ))}
    </div>
  );
};

const ConversationItem = ({ message }: { message: ConversationMessage }) => {
  if (message.role === "trace") {
    return (
      <div className="trace-message">
        <TerminalSquare size={15} aria-hidden="true" />
        <div>
          <strong>{displaySafeText(message.content)}</strong>
          {message.detail ?
            <span>{displaySafeText(message.detail)}</span>
          : null}
          {message.technicalDetail ?
            <details className="trace-details">
              <summary>Exact action details</summary>
              <pre tabIndex={0}>{displaySafeText(message.technicalDetail)}</pre>
            </details>
          : null}
        </div>
      </div>
    );
  }
  return (
    <article className={`conversation-message ${message.role}`}>
      <div className="conversation-meta">
        <small>{displaySafeText(message.label)}</small>
        {message.detail ?
          <span>{displaySafeText(message.detail)}</span>
        : null}
      </div>
      {message.role === "agent" ?
        <SafeMarkdown content={message.content} />
      : <p>{displaySafeText(message.content)}</p>}
    </article>
  );
};

const StreamingMessage = ({ content }: { content: string }) => (
  <article
    aria-label="Agent response in progress"
    className="conversation-message agent streaming-message"
  >
    <div className="conversation-meta">
      <small>Agent</small>
      <span>Responding</span>
    </div>
    <SafeMarkdown content={content} />
    <span aria-hidden="true" className="streaming-cursor" />
  </article>
);

const ActionHistory = ({
  actions,
  actionPresentation,
}: {
  actions: ProjectionActionRecord[];
  actionPresentation: ActionPresentation | null;
}) => {
  if (actions.length === 0) return null;
  return (
    <section aria-label="Agent actions" className="action-history">
      <h2>Agent actions</h2>
      <ol>
        {actions.map((action) => {
          const risk = actionRiskPresentation(action.risk, actionPresentation);
          const tool = actionToolLabel(action.toolName, actionPresentation);
          return (
            <li key={action.toolCallId}>
              <ActionStateIcon state={action.state} />
              <div>
                <div className="action-history-heading">
                  <strong>{displaySafeText(actionHeading(action))}</strong>
                  <Badge variant="secondary">
                    {actionStateLabel(action.state, actionPresentation)}
                  </Badge>
                </div>
                <div className="approval-action-meta">
                  <span>{tool}</span>
                  <Badge className={risk.className} variant="outline">
                    {risk.label}
                  </Badge>
                  {action.outcome !== null ?
                    <span>
                      Exit {action.outcome.exitCode} ·{" "}
                      {displaySafeText(action.outcome.summary)}
                    </span>
                  : null}
                  {action.groupId !== null ?
                    <span>
                      Complete action set ·{" "}
                      {action.decision ?? "decision pending"}
                    </span>
                  : action.decision !== null ?
                    <span>{action.decision} by automatic policy</span>
                  : null}
                </div>
                {action.affectedPaths.length > 0 ?
                  <p>
                    {displaySafeText(
                      action.affectedPaths
                        .map(
                          (path) =>
                            `${path.effect}: ${path.path} (${path.provenance})`,
                        )
                        .join(", "),
                    )}
                  </p>
                : null}
                {Object.keys(action.arguments).length > 0 ?
                  <details className="trace-details">
                    <summary>Exact arguments</summary>
                    <pre tabIndex={0}>
                      {displaySafeText(
                        JSON.stringify(action.arguments, null, 2),
                      )}
                    </pre>
                  </details>
                : null}
                {action.outcome?.result ?
                  <details className="trace-details">
                    <summary>
                      Action output
                      {action.outcome.resultTruncated ? " (truncated)" : ""}
                    </summary>
                    <pre tabIndex={0}>
                      {displaySafeText(action.outcome.result)}
                    </pre>
                  </details>
                : null}
                <ActionTechnicalDetails action={action} />
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
};

const ActionStateIcon = ({
  state,
}: {
  state: ProjectionActionRecord["state"];
}) =>
  state === "succeeded" ?
    <CircleCheck
      aria-hidden="true"
      className="action-state-success"
      size={17}
    />
  : state === "failed" || state === "rejected" || state === "outcome-unknown" ?
    <CircleX aria-hidden="true" className="action-state-error" size={17} />
  : <Clock3 aria-hidden="true" className="action-state-pending" size={17} />;

const ActionTechnicalDetails = ({
  action,
}: {
  action: ProjectionActionRecord;
}) => {
  const details = [
    `Tool call: ${action.toolCallId}`,
    ...(action.actionId === null ? [] : [`Action: ${action.actionId}`]),
    ...(action.groupId === null ? [] : [`Action set: ${action.groupId}`]),
  ];
  return (
    <details className="trace-details">
      <summary>Technical details</summary>
      <pre tabIndex={0}>{displaySafeText(details.join("\n"))}</pre>
    </details>
  );
};

const actionHeading = (action: ProjectionActionRecord) => {
  if (action.details.kind === "terminal") {
    return `$ ${action.details.command}`;
  }
  if (action.details.kind === "file-editor") {
    return `${action.details.operation.replace("_", " ")} ${
      action.details.path ?? "path unavailable"
    }`;
  }
  if (action.details.kind === "task") {
    return (
      action.details.description ??
      action.details.subagentType ??
      action.summary
    );
  }
  return action.summary;
};

const RuntimeStatus = ({
  projection,
}: {
  projection: SessionProjection | null;
}) => {
  if (
    projection === null ||
    (projection.lifecycle.status === "idle" &&
      projection.researcherStatus.code === "ready" &&
      projection.taskPlan.length === 0 &&
      projection.usage === null &&
      projection.subagents.length === 0)
  ) {
    return null;
  }
  const completedTasks = projection.taskPlan.filter(
    (task) => task.status === "done",
  ).length;
  const totalTokens =
    (projection.usage?.promptTokens ?? 0) +
    (projection.usage?.completionTokens ?? 0);
  return (
    <section aria-label="Agent status" className="runtime-status" role="status">
      <div className="runtime-status-heading">
        {projection.lifecycle.status === "running" ?
          <LoaderCircle
            aria-hidden="true"
            className="request-activity-icon"
            size={16}
          />
        : <ListChecks aria-hidden="true" size={16} />}
        <strong>{projection.researcherStatus.label}</strong>
      </div>
      <p>{projection.researcherStatus.detail}</p>
      {projection.taskPlan.length > 0 ?
        <details>
          <summary>
            Plan: {completedTasks} of {projection.taskPlan.length} complete
          </summary>
          <ol>
            {projection.taskPlan.map((task, index) => (
              <li key={`${index}-${task.title}`}>
                <span>{displaySafeText(task.title)}</span>
                <small>{task.statusLabel}</small>
              </li>
            ))}
          </ol>
        </details>
      : null}
      <div className="runtime-status-metrics">
        {projection.usage ?
          <span>
            {totalTokens.toLocaleString()} tokens ·{" "}
            {displaySafeText(projection.usage.modelName)}
            {projection.usage.contextWindow === null ?
              ""
            : ` · ${projection.usage.contextWindow.toLocaleString()} context limit`
            }
            {projection.usage.accumulatedCost <= 0 ?
              ""
            : ` · $${projection.usage.accumulatedCost.toFixed(2)} reported cost`
            }
            {` · ${projection.usage.callCount.toLocaleString()} calls`}
          </span>
        : null}
      </div>
      {projection.usageByPurpose.length > 0 ?
        <details>
          <summary>Model activity</summary>
          <ul>
            {projection.usageByPurpose.map((usage) => (
              <li key={usage.usageId}>
                <span>{displaySafeText(usage.purposeLabel)}</span>
                <small>
                  {usage.callCount.toLocaleString()} calls ·{" "}
                  {(
                    usage.promptTokens + usage.completionTokens
                  ).toLocaleString()}{" "}
                  tokens
                </small>
              </li>
            ))}
          </ul>
        </details>
      : null}
      {projection.subagents.length > 0 ?
        <details className="runtime-subagents">
          <summary>
            {projection.subagents.length}{" "}
            {projection.subagents.length === 1 ? "specialist" : "specialists"}
          </summary>
          <ul>
            {projection.subagents.map((subagent) => (
              <li key={subagent.invocationId}>
                <span>
                  {displaySafeText(subagent.roleLabel)} ({subagent.statusLabel})
                </span>
                {subagent.taskSummary ?
                  <small>Task: {displaySafeText(subagent.taskSummary)}</small>
                : null}
                {subagent.resultSummary ?
                  <small>
                    Result: {displaySafeText(subagent.resultSummary)}
                  </small>
                : null}
                <details className="trace-details">
                  <summary>Technical details</summary>
                  <pre tabIndex={0}>
                    {displaySafeText(
                      [
                        `Invocation: ${subagent.invocationId}`,
                        `Parent session: ${subagent.parentSessionId}`,
                        `Parent action: ${subagent.parentActionId}`,
                      ].join("\n"),
                    )}
                  </pre>
                </details>
              </li>
            ))}
          </ul>
        </details>
      : null}
    </section>
  );
};

const ApprovalRequest = ({
  actionModeLabel,
  actionPresentation,
  approval,
  busy,
  canApprove,
  canDeny,
  onDecision,
}: {
  actionModeLabel: string | null;
  actionPresentation: ActionPresentation | null;
  approval: ProjectionApprovalGroup;
  busy: boolean;
  canApprove: boolean;
  canDeny: boolean;
  onDecision: (
    decision: "approve" | "deny",
    approval: ProjectionApprovalGroup,
  ) => void;
}) => {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, [approval.groupId]);
  const countLabel = actionCountLabel(approval.actions.length);
  const allowLabel =
    approval.actions.length === 1 ? "Allow Once" : "Allow All Once";
  const rejectLabel = approval.actions.length === 1 ? "Reject" : "Reject All";
  return (
    <section
      className="approval-request"
      aria-labelledby="action-review-heading"
      aria-busy={busy}
    >
      <div className="approval-copy">
        <div className="approval-introduction">
          <span className="approval-icon" aria-hidden="true">
            <ShieldAlert size={18} />
          </span>
          <div>
            <div className="approval-heading">
              <small>Action Review</small>
              <Badge variant="secondary">{countLabel}</Badge>
              {actionModeLabel ?
                <span>Paused by {actionModeLabel}</span>
              : null}
            </div>
            <h2 id="action-review-heading" ref={headingRef} tabIndex={-1}>
              One Decision for This Action Set
            </h2>
            <p>
              These actions were proposed together. Allowing runs every action
              once; rejecting runs none of them.
            </p>
          </div>
        </div>
        <ol className="approval-batch-list" role="list">
          {approval.actions.map((control, index) => {
            const risk = actionRiskPresentation(
              control.risk,
              actionPresentation,
            );
            const tool = actionToolLabel(control.toolName, actionPresentation);
            return (
              <li key={control.toolCallId}>
                <span className="approval-action-index" aria-hidden="true">
                  {index + 1}
                </span>
                <div className="approval-action-content">
                  <strong>{displaySafeText(control.summary || tool)}</strong>
                  <div className="approval-action-meta">
                    <span>{tool}</span>
                    <Badge className={risk.className} variant="outline">
                      {risk.label}
                    </Badge>
                  </div>
                  {Object.keys(control.arguments).length > 0 ?
                    <details className="approval-details">
                      <summary>Review Exact Arguments</summary>
                      <pre tabIndex={0} aria-label={`Arguments for ${tool}`}>
                        {displaySafeText(
                          JSON.stringify(control.arguments, null, 2),
                        )}
                      </pre>
                    </details>
                  : null}
                </div>
              </li>
            );
          })}
        </ol>
      </div>
      <div className="approval-actions">
        <span>
          Your decision applies to all <strong>{countLabel}</strong>.
        </span>
        <Button
          aria-label={`Reject ${countLabel}`}
          disabled={busy || !canDeny}
          size="sm"
          variant="outline"
          onClick={() => onDecision("deny", approval)}
        >
          <Ban size={16} />
          {rejectLabel}
        </Button>
        <Button
          aria-label={`Allow ${countLabel} once`}
          disabled={busy || !canApprove}
          size="sm"
          onClick={() => onDecision("approve", approval)}
        >
          <Check size={16} />
          {allowLabel}
        </Button>
      </div>
    </section>
  );
};
