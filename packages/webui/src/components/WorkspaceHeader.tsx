/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Badge } from "@schmiedmayerlab/grove-design-system/components/Badge";
import { Button } from "@schmiedmayerlab/grove-design-system/components/Button";
import { Input } from "@schmiedmayerlab/grove-design-system/components/Input";
import { Tooltip } from "@schmiedmayerlab/grove-design-system/components/Tooltip";
import { LoaderCircle, Menu, Pencil, ShieldCheck } from "lucide-react";
import { useState } from "react";
import type { ProjectionResearcherStatus, SessionSummary } from "../types";
import { displaySafeText } from "./SafeMarkdown";

interface WorkspaceHeaderProps {
  actionModeLabel: string;
  modelDetail: string | null;
  modelLabel: string;
  modelStatus: "checking" | "denied" | "ready" | "setup";
  platformLabel: string;
  projectLabel: string;
  researcherStatus: ProjectionResearcherStatus | null;
  requestStatus: "idle" | "busy" | "error";
  session: SessionSummary | null;
  onOpenActionReview: () => void;
  onOpenMenu: () => void;
  onRename: (title: string) => void;
}

export const WorkspaceHeader = ({
  actionModeLabel,
  modelDetail,
  modelLabel,
  modelStatus,
  platformLabel,
  projectLabel,
  researcherStatus,
  requestStatus,
  session,
  onOpenActionReview,
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
          <StatusBadge
            modelStatus={modelStatus}
            researcherStatus={researcherStatus}
            requestStatus={requestStatus}
          />
        </div>
      </div>

      <dl className="context-strip" aria-label="Session context">
        <ContextFact label="Project" value={projectLabel} />
        <ContextFact label="Platform" value={platformLabel} />
        <ContextFact
          label="Model"
          value={modelLabel}
          detail={modelDetail ?? undefined}
        />
        <ContextFact
          label="Action review"
          value={actionModeLabel}
          icon={<ShieldCheck size={14} />}
          onActivate={onOpenActionReview}
        />
      </dl>
    </header>
  );
};

const ContextFact = ({
  detail,
  icon,
  label,
  onActivate,
  value,
}: {
  detail?: string;
  icon?: React.ReactNode;
  label: string;
  onActivate?: () => void;
  value: string;
}) => {
  const content = (
    <>
      {icon}
      <span title={value}>{value}</span>
    </>
  );
  return (
    <div className="context-fact">
      <dt>{label}</dt>
      <dd title={detail}>
        {onActivate ?
          <Button
            aria-label={`Open ${label.toLowerCase()} settings`}
            className="context-fact-button"
            size="sm"
            variant="ghost"
            onClick={onActivate}
          >
            {content}
          </Button>
        : content}
      </dd>
    </div>
  );
};

const StatusBadge = ({
  modelStatus,
  researcherStatus,
  requestStatus,
}: {
  modelStatus: "checking" | "denied" | "ready" | "setup";
  researcherStatus: ProjectionResearcherStatus | null;
  requestStatus: "idle" | "busy" | "error";
}) => {
  if (requestStatus === "error" || modelStatus === "denied") {
    return <Badge variant="destructiveLight">Needs attention</Badge>;
  }
  if (requestStatus === "busy") {
    return (
      <Badge variant="secondary">
        <LoaderCircle
          aria-hidden="true"
          className="request-activity-icon"
          size={13}
        />
        Working
      </Badge>
    );
  }
  if (modelStatus === "checking") {
    return <Badge variant="secondary">Checking model</Badge>;
  }
  if (modelStatus === "setup") {
    return <Badge variant="secondary">Setup needed</Badge>;
  }
  if (researcherStatus?.tone === "danger") {
    return (
      <Badge variant="destructiveLight">
        {displaySafeText(researcherStatus.label)}
      </Badge>
    );
  }
  return (
    <Badge variant="secondary">
      {researcherStatus === null ?
        "Loading session"
      : displaySafeText(researcherStatus.label)}
    </Badge>
  );
};
