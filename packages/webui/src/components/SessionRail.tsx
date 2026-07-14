/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Button } from "@stanfordspezi/spezi-web-design-system/components/Button";
import { StatusDot } from "@stanfordspezi/spezi-web-design-system/components/StatusDot";
import {
  Activity,
  BookOpen,
  Download,
  Plus,
  Settings,
  Sprout,
} from "lucide-react";
import type { SessionSummary } from "../types";

export type UtilityPanel = "activity" | "settings" | "skills" | null;

interface SessionRailProps {
  activePanel: UtilityPanel;
  selectedSessionId: string | null;
  sessions: SessionSummary[];
  onExportAudit: () => void;
  onNewSession: () => void;
  onOpenPanel: (panel: Exclude<UtilityPanel, null>) => void;
  onSelectSession: (sessionId: string) => void;
}

export const SessionRail = (props: SessionRailProps) => (
  <aside className="session-rail" aria-label="Heartwood sessions">
    <SessionRailContent {...props} />
  </aside>
);

export const SessionRailContent = ({
  activePanel,
  selectedSessionId,
  sessions,
  onExportAudit,
  onNewSession,
  onOpenPanel,
  onSelectSession,
}: SessionRailProps) => (
  <div className="session-rail-content">
    <div className="brand-lockup">
      <span className="brand-mark" aria-hidden="true">
        <Sprout size={18} />
      </span>
      <strong>Heartwood</strong>
    </div>

    <Button
      className="new-session-button"
      variant="outline"
      onClick={onNewSession}
    >
      <Plus size={17} />
      New analysis
    </Button>

    <nav className="session-navigation" aria-label="Sessions">
      <div className="rail-section-label">Sessions</div>
      <div className="session-list">
        {sessions.length === 0 ?
          <p className="session-list-empty">No saved sessions</p>
        : sessions.map((session) => (
            <button
              aria-label={`${session.title}, ${sessionStatusLabel(session.status)}, ${sessionMeta(session)}`}
              aria-current={
                session.session_id === selectedSessionId ? "page" : undefined
              }
              className="session-list-item"
              key={session.session_id}
              type="button"
              onClick={() => onSelectSession(session.session_id)}
            >
              <StatusDot
                aria-hidden={true}
                className="session-status-dot"
                size="sm"
                status={sessionStatus(session.status)}
              />
              <span className="session-list-copy">
                <strong>{session.title}</strong>
                <small>{sessionMeta(session)}</small>
              </span>
            </button>
          ))
        }
      </div>
    </nav>

    <nav className="rail-tools" aria-label="Project tools">
      <Button
        aria-pressed={activePanel === "skills"}
        variant={activePanel === "skills" ? "secondary" : "ghost"}
        onClick={() => onOpenPanel("skills")}
      >
        <BookOpen size={17} />
        Skills
      </Button>
      <Button
        aria-pressed={activePanel === "activity"}
        variant={activePanel === "activity" ? "secondary" : "ghost"}
        onClick={() => onOpenPanel("activity")}
      >
        <Activity size={17} />
        Activity &amp; audit
      </Button>
      <Button
        aria-pressed={activePanel === "settings"}
        variant={activePanel === "settings" ? "secondary" : "ghost"}
        onClick={() => onOpenPanel("settings")}
      >
        <Settings size={17} />
        Settings
      </Button>
      <Button variant="ghost" onClick={onExportAudit}>
        <Download size={17} />
        Export audit
      </Button>
    </nav>
  </div>
);

const sessionMeta = (session: SessionSummary): string => {
  const date = new Date(session.updated_at);
  const timestamp =
    Number.isNaN(date.valueOf()) ? "Unknown time" : formatRelativeDate(date);
  return session.event_count === 0 ?
      timestamp
    : `${timestamp} · ${session.event_count} events`;
};

const formatRelativeDate = (date: Date): string => {
  const elapsed = Date.now() - date.valueOf();
  if (elapsed >= 0 && elapsed < 60_000) return "Just now";
  if (elapsed >= 0 && elapsed < 3_600_000)
    return `${Math.floor(elapsed / 60_000)} min ago`;
  if (elapsed >= 0 && elapsed < 86_400_000)
    return `${Math.floor(elapsed / 3_600_000)} hr ago`;
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
  }).format(date);
};

const sessionStatus = (
  status: SessionSummary["status"],
): "default" | "destructive" | "primary" | "success" | "warning" => {
  if (status === "error") return "destructive";
  if (status === "idle") return "success";
  if (status === "paused") return "primary";
  if (status === "waiting") return "warning";
  return "default";
};

const sessionStatusLabel = (status: SessionSummary["status"]): string => {
  if (status === "error") return "Needs attention";
  if (status === "paused") return "Paused";
  if (status === "waiting") return "Approval needed";
  return "Ready";
};
