/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Badge } from "@stanfordspezi/spezi-web-design-system/components/Badge";
import { Button } from "@stanfordspezi/spezi-web-design-system/components/Button";
import { Input } from "@stanfordspezi/spezi-web-design-system/components/Input";
import { Tooltip } from "@stanfordspezi/spezi-web-design-system/components/Tooltip";
import { Database, Menu, Pencil, ShieldCheck } from "lucide-react";
import { useState } from "react";
import type {
  ActionSettings,
  ModelProfile,
  SessionContext,
  SessionSummary,
} from "../types";

interface WorkspaceHeaderProps {
  actionSettings: ActionSettings | null;
  activeProfile: ModelProfile | null;
  context: SessionContext;
  requestStatus: "idle" | "busy" | "error";
  session: SessionSummary | null;
  onDetect: () => void;
  onOpenMenu: () => void;
  onRename: (title: string) => void;
}

export const WorkspaceHeader = ({
  actionSettings,
  activeProfile,
  context,
  requestStatus,
  session,
  onDetect,
  onOpenMenu,
  onRename,
}: WorkspaceHeaderProps) => {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");

  const commitTitle = () => {
    const next = title.trim();
    if (next && next !== session?.title) onRename(next);
    else setTitle(session?.title ?? "");
    setEditing(false);
  };

  return (
    <header className="workspace-header">
      <div className="workspace-title-row">
        <Tooltip tooltip="Open sessions">
          <Button
            aria-label="Open sessions"
            className="mobile-menu-button"
            size="sm"
            variant="outline"
            onClick={onOpenMenu}
          >
            <Menu size={17} />
          </Button>
        </Tooltip>
        <div className="workspace-title">
          {editing ?
            <Input
              aria-label="Session title"
              autoFocus
              value={title}
              onBlur={commitTitle}
              onChange={(event) => setTitle(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") commitTitle();
                if (event.key === "Escape") {
                  setTitle(session?.title ?? "");
                  setEditing(false);
                }
              }}
            />
          : <>
              <h1>{session?.title ?? "Loading session"}</h1>
              {session ?
                <Tooltip tooltip="Rename session">
                  <Button
                    aria-label="Rename session"
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setTitle(session.title);
                      setEditing(true);
                    }}
                  >
                    <Pencil size={15} />
                  </Button>
                </Tooltip>
              : null}
            </>
          }
        </div>
        <div className="workspace-actions">
          <StatusBadge requestStatus={requestStatus} session={session} />
          <Tooltip tooltip="Detect platform and dataset">
            <Button
              aria-label="Detect environment"
              disabled={session === null}
              size="sm"
              variant="outline"
              onClick={onDetect}
            >
              <Database size={17} />
            </Button>
          </Tooltip>
        </div>
      </div>

      <dl className="context-strip" aria-label="Session context">
        <ContextFact
          label="Platform"
          value={context.platform ?? "Not detected"}
        />
        <ContextFact
          label="Dataset"
          value={context.dataset ?? "Not detected"}
        />
        <ContextFact
          label="Model"
          value={activeProfile?.profile_id ?? "Not configured"}
          detail={activeProfile?.model}
        />
        <ContextFact
          label="Approvals"
          value={approvalLabel(actionSettings)}
          icon={<ShieldCheck size={14} />}
        />
        <ContextFact label="Boundary evidence" value="Planned" />
        <ContextFact label="Workflow progress" value="Planned" />
      </dl>
    </header>
  );
};

const ContextFact = ({
  detail,
  icon,
  label,
  value,
}: {
  detail?: string;
  icon?: React.ReactNode;
  label: string;
  value: string;
}) => (
  <div className="context-fact">
    <dt>{label}</dt>
    <dd title={detail}>
      {icon}
      {value}
    </dd>
  </div>
);

const StatusBadge = ({
  requestStatus,
  session,
}: {
  requestStatus: "idle" | "busy" | "error";
  session: SessionSummary | null;
}) => {
  const status =
    requestStatus === "idle" ? (session?.status ?? "idle") : requestStatus;
  return (
    <Badge variant={status === "error" ? "destructiveLight" : "secondary"}>
      {status === "busy" ? "Working" : statusLabel(status)}
    </Badge>
  );
};

const statusLabel = (status: string): string =>
  ({
    empty: "Ready",
    error: "Needs attention",
    idle: "Ready",
    paused: "Paused",
    waiting: "Approval needed",
  })[status] ?? status;

const approvalLabel = (settings: ActionSettings | null): string => {
  if (settings === null) return "Loading";
  return (
    settings.modes.find((mode) => mode.mode === settings.confirmation_mode)
      ?.label ?? settings.confirmation_mode
  );
};
