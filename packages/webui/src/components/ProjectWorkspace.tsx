/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Badge } from "@schmiedmayerlab/grove-design-system/components/Badge";
import {
  AlertTriangle,
  File,
  FileDiff,
  Folder,
  LoaderCircle,
} from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import type { HeartwoodClient } from "../client";
import type {
  WorkspaceChanges,
  WorkspaceDiff,
  WorkspaceFile,
  WorkspaceTree,
} from "../types";

const CodeViewer = lazy(() =>
  import("./CodeViewer").then((module) => ({ default: module.CodeViewer })),
);
const DiffViewer = lazy(() =>
  import("./CodeViewer").then((module) => ({ default: module.DiffViewer })),
);

interface ProjectWorkspaceProps {
  client: HeartwoodClient;
  mode: "changes" | "files";
  revision: number;
  sessionId: string;
}

interface OverviewSnapshot {
  changes?: WorkspaceChanges;
  error?: string;
  key: string;
  tree?: WorkspaceTree;
}

interface PreviewSnapshot {
  diff?: WorkspaceDiff;
  error?: string;
  file?: WorkspaceFile;
  key: string;
}

export const ProjectWorkspace = ({
  client,
  mode,
  revision,
  sessionId,
}: ProjectWorkspaceProps) => {
  const [selection, setSelection] = useState<{
    key: string;
    path: string;
  } | null>(null);
  const [overview, setOverview] = useState<OverviewSnapshot | null>(null);
  const [preview, setPreview] = useState<PreviewSnapshot | null>(null);
  const selectionKey = `${sessionId}:${mode}`;
  const selectedPath = selection?.key === selectionKey ? selection.path : null;
  const overviewKey = JSON.stringify([sessionId, mode, revision]);
  const previewKey =
    selectedPath === null ? null : `${overviewKey}:${selectedPath}`;

  useEffect(() => {
    let active = true;
    if (mode === "files") {
      void client
        .getWorkspaceTree(sessionId)
        .then((response) => {
          if (!active) return;
          setOverview({
            key: overviewKey,
            tree: response,
          });
        })
        .catch((caught: unknown) => {
          if (active) {
            setOverview({ error: errorMessage(caught), key: overviewKey });
          }
        });
    } else {
      void client
        .getWorkspaceChanges(sessionId)
        .then((response) => {
          if (!active) return;
          setOverview({
            changes: response,
            key: overviewKey,
          });
        })
        .catch((caught: unknown) => {
          if (active) {
            setOverview({ error: errorMessage(caught), key: overviewKey });
          }
        });
    }
    return () => {
      active = false;
    };
  }, [client, mode, overviewKey, sessionId]);

  useEffect(() => {
    if (selectedPath === null || previewKey === null) return;
    let active = true;
    if (mode === "files") {
      void client
        .getWorkspaceFile(sessionId, selectedPath)
        .then((response) => {
          if (active) setPreview({ file: response, key: previewKey });
        })
        .catch((caught: unknown) => {
          if (active) {
            setPreview({ error: errorMessage(caught), key: previewKey });
          }
        });
    } else {
      void client
        .getWorkspaceDiff(sessionId, selectedPath)
        .then((response) => {
          if (active) setPreview({ diff: response, key: previewKey });
        })
        .catch((caught: unknown) => {
          if (active) {
            setPreview({ error: errorMessage(caught), key: previewKey });
          }
        });
    }
    return () => {
      active = false;
    };
  }, [client, mode, previewKey, selectedPath, sessionId]);

  const loading = overview?.key !== overviewKey;
  const tree = overview?.key === overviewKey ? (overview.tree ?? null) : null;
  const changes =
    overview?.key === overviewKey ? (overview.changes ?? null) : null;
  const overviewError =
    overview?.key === overviewKey ? (overview.error ?? null) : null;
  const file =
    previewKey !== null && preview?.key === previewKey ?
      (preview.file ?? null)
    : null;
  const diff =
    previewKey !== null && preview?.key === previewKey ?
      (preview.diff ?? null)
    : null;
  const previewError =
    previewKey !== null && preview?.key === previewKey ?
      (preview.error ?? null)
    : null;

  const entries =
    mode === "files" ? (tree?.entries ?? []) : (changes?.changes ?? []);
  const emptyMessage =
    mode === "files" ?
      "No project files are available."
    : (changes?.message ?? "No changes are available.");
  const selectPath = (path: string) =>
    setSelection({ key: selectionKey, path });

  return (
    <section
      aria-label={mode === "files" ? "Project files" : "Project changes"}
      className="project-workspace"
    >
      <aside className="project-browser">
        <div className="project-browser-heading">
          <strong>{mode === "files" ? "Project files" : "Changes"}</strong>
          {mode === "changes" && changes !== null ?
            <Badge variant="secondary">{sourceLabel(changes)}</Badge>
          : null}
        </div>
        {loading ?
          <WorkspaceState icon="loading" message="Loading project workspace" />
        : overviewError !== null && entries.length === 0 ?
          <WorkspaceState icon="error" message={overviewError} />
        : entries.length === 0 ?
          <WorkspaceState icon="empty" message={emptyMessage} />
        : <ul
            aria-label={mode === "files" ? "Project tree" : "Changed files"}
            className="project-entry-list"
          >
            {mode === "files" ?
              tree?.entries.map((entry) => (
                <li key={entry.path}>
                  <button
                    aria-current={selectedPath === entry.path || undefined}
                    disabled={entry.kind !== "file"}
                    style={{ paddingLeft: `${10 + (entry.depth - 1) * 16}px` }}
                    type="button"
                    onClick={() => selectPath(entry.path)}
                  >
                    {entry.kind === "directory" ?
                      <Folder aria-hidden="true" size={15} />
                    : <File aria-hidden="true" size={15} />}
                    <span>{entry.name}</span>
                    {entry.kind === "unsupported" ?
                      <small>Unavailable</small>
                    : null}
                  </button>
                </li>
              ))
            : changes?.changes.map((change) => (
                <li key={change.path}>
                  <button
                    aria-label={`${change.path}, ${changeStatusLabel(
                      change.status,
                    )}`}
                    aria-current={selectedPath === change.path || undefined}
                    type="button"
                    onClick={() => selectPath(change.path)}
                  >
                    <FileDiff aria-hidden="true" size={15} />
                    <span>{change.path}</span>
                    <small>{changeStatusLabel(change.status)}</small>
                  </button>
                </li>
              ))
            }
          </ul>
        }
        {tree?.truncated === true || changes?.truncated === true ?
          <p className="workspace-limit-message">
            The bounded workspace view was truncated.
          </p>
        : null}
        {changes?.message && changes.changes.length > 0 ?
          <p className="workspace-limit-message">{changes.message}</p>
        : null}
      </aside>
      <div className="project-preview">
        {previewError !== null ?
          <div className="workspace-inline-error" role="alert">
            <AlertTriangle aria-hidden="true" size={16} />
            <span>{previewError}</span>
          </div>
        : mode === "files" ?
          <FilePreview file={file} selectedPath={selectedPath} />
        : <ChangePreview diff={diff} selectedPath={selectedPath} />}
      </div>
    </section>
  );
};

const FilePreview = ({
  file,
  selectedPath,
}: {
  file: WorkspaceFile | null;
  selectedPath: string | null;
}) => {
  if (selectedPath === null) {
    return (
      <WorkspaceState
        icon="empty"
        message="Select a text file to inspect it."
      />
    );
  }
  if (file === null) {
    return (
      <WorkspaceState icon="loading" message={`Loading ${selectedPath}`} />
    );
  }
  if (
    (file.status === "available" || file.status === "truncated") &&
    file.content !== null
  ) {
    return (
      <>
        <PreviewHeader path={file.path} status={file.status} />
        <Suspense
          fallback={
            <WorkspaceState icon="loading" message={`Preparing ${file.path}`} />
          }
        >
          <CodeViewer content={file.content} path={file.path} />
        </Suspense>
        {file.message ?
          <p className="workspace-limit-message">{file.message}</p>
        : null}
      </>
    );
  }
  return (
    <WorkspaceState
      icon="error"
      message={file.message ?? `This ${file.status} file cannot be displayed.`}
    />
  );
};

const ChangePreview = ({
  diff,
  selectedPath,
}: {
  diff: WorkspaceDiff | null;
  selectedPath: string | null;
}) => {
  if (selectedPath === null) {
    return (
      <WorkspaceState
        icon="empty"
        message="Select a changed file to inspect it."
      />
    );
  }
  if (diff === null) {
    return (
      <WorkspaceState icon="loading" message={`Loading ${selectedPath}`} />
    );
  }
  if (
    (diff.status === "available" ||
      diff.status === "non-git" ||
      diff.status === "truncated") &&
    (diff.original !== null || diff.modified !== null)
  ) {
    return (
      <>
        <PreviewHeader path={diff.path} status={diff.status} />
        <Suspense
          fallback={
            <WorkspaceState icon="loading" message={`Preparing ${diff.path}`} />
          }
        >
          <DiffViewer
            modified={diff.modified ?? ""}
            original={diff.original ?? ""}
            path={diff.path}
          />
        </Suspense>
        {diff.message ?
          <p className="workspace-limit-message">{diff.message}</p>
        : null}
      </>
    );
  }
  return (
    <WorkspaceState
      icon="error"
      message={
        diff.message ?? `This ${diff.status} change cannot be displayed.`
      }
    />
  );
};

const PreviewHeader = ({ path, status }: { path: string; status: string }) => (
  <header className="project-preview-heading">
    <strong>{path}</strong>
    <Badge variant="secondary">{status.replace("-", " ")}</Badge>
  </header>
);

const WorkspaceState = ({
  icon,
  message,
}: {
  icon: "empty" | "error" | "loading";
  message: string;
}) => (
  <div className="workspace-state" role={icon === "error" ? "alert" : "status"}>
    {icon === "loading" ?
      <LoaderCircle
        aria-hidden="true"
        className="request-activity-icon"
        size={19}
      />
    : icon === "error" ?
      <AlertTriangle aria-hidden="true" size={19} />
    : <File aria-hidden="true" size={19} />}
    <span>{message}</span>
  </div>
);

const sourceLabel = (changes: WorkspaceChanges) =>
  ({
    git: "Git",
    "session-actions": "Session actions",
    unavailable: "Unavailable",
  })[changes.source];

const changeStatusLabel = (
  status: WorkspaceChanges["changes"][number]["status"],
) => ({ added: "Added", deleted: "Deleted", modified: "Modified" })[status];

const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "Workspace inspection failed.";
