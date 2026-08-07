/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { HeartwoodClient } from "../client";
import type {
  WorkspaceChanges,
  WorkspaceDiff,
  WorkspaceFile,
  WorkspaceTree,
} from "../types";
import { ProjectWorkspace } from "./ProjectWorkspace";

const limits = {
  max_change_entries: 100,
  max_diff_bytes: 128_000,
  max_file_bytes: 64_000,
  max_file_lines: 2_000,
  max_tree_depth: 8,
  max_tree_entries: 1_000,
};

const clientWith = (
  methods: Partial<
    Pick<
      HeartwoodClient,
      | "getWorkspaceChanges"
      | "getWorkspaceDiff"
      | "getWorkspaceFile"
      | "getWorkspaceTree"
    >
  >,
) => methods as HeartwoodClient;

const emptyTree = (): WorkspaceTree => ({
  schema_version: "heartwood.workspace-tree.v1",
  path: ".",
  status: "available",
  entries: [],
  truncated: false,
  limits,
});

const emptyChanges = (): WorkspaceChanges => ({
  schema_version: "heartwood.workspace-changes.v1",
  status: "available",
  source: "git",
  changes: [],
  truncated: false,
  message: null,
  limits,
});

describe("ProjectWorkspace", () => {
  it("presents a bounded tree, unsupported entries, and truncated text", async () => {
    const tree: WorkspaceTree = {
      ...emptyTree(),
      status: "truncated",
      truncated: true,
      entries: [
        {
          path: "results",
          name: "results",
          kind: "directory",
          depth: 1,
          size_bytes: null,
        },
        {
          path: "results/socket",
          name: "socket",
          kind: "unsupported",
          depth: 2,
          size_bytes: null,
        },
        {
          path: "analysis.py",
          name: "analysis.py",
          kind: "file",
          depth: 1,
          size_bytes: 24,
        },
      ],
    };
    const file: WorkspaceFile = {
      schema_version: "heartwood.workspace-file.v1",
      path: "analysis.py",
      status: "truncated",
      content: "answer = 42\n",
      size_bytes: 100_000,
      bytes_read: 12,
      line_count: 1,
      truncated: true,
      message: "Only the bounded prefix is shown.",
    };
    const client = clientWith({
      getWorkspaceTree: vi.fn().mockResolvedValue(tree),
      getWorkspaceFile: vi.fn().mockResolvedValue(file),
    });

    render(
      <ProjectWorkspace
        client={client}
        mode="files"
        revision={1}
        sessionId="synthetic"
      />,
    );

    const workspace = await screen.findByRole("region", {
      name: "Project files",
    });
    expect(
      within(workspace).getByRole("button", { name: "results" }),
    ).toBeDisabled();
    expect(
      within(workspace).getByRole("button", { name: /socket/u }),
    ).toBeDisabled();
    expect(within(workspace).getByText("Unavailable")).toBeInTheDocument();
    expect(
      within(workspace).getByText("The bounded workspace view was truncated."),
    ).toBeInTheDocument();

    fireEvent.click(
      within(workspace).getByRole("button", { name: "analysis.py" }),
    );
    const preview = await screen.findByRole("region", {
      name: "Read-only file: analysis.py",
    });
    expect(preview).toHaveTextContent("answer = 42");
    expect(
      within(preview).getByRole("textbox", {
        name: "Read-only file contents: analysis.py",
      }),
    ).toHaveAttribute("aria-readonly", "true");
    expect(
      within(preview).getByLabelText("Scrollable file contents: analysis.py"),
    ).toHaveAttribute("tabindex", "0");
    expect(
      within(workspace).getByText("Only the bounded prefix is shown."),
    ).toBeInTheDocument();
  });

  it("keeps the newest changed-file selection when requests complete out of order", async () => {
    let resolveFirst: ((response: WorkspaceDiff) => void) | undefined;
    const first = new Promise<WorkspaceDiff>((resolve) => {
      resolveFirst = resolve;
    });
    const changes: WorkspaceChanges = {
      ...emptyChanges(),
      source: "session-actions",
      status: "truncated",
      truncated: true,
      changes: [
        {
          path: "first.py",
          status: "added",
          source: "session-action",
          action_ids: ["action-1"],
        },
        {
          path: "second.py",
          status: "deleted",
          source: "session-action",
          action_ids: ["action-2"],
        },
        {
          path: "third.py",
          status: "modified",
          source: "session-action",
          action_ids: ["action-3"],
        },
        {
          path: "unsupported.bin",
          status: "modified",
          source: "session-action",
          action_ids: ["action-4"],
        },
        {
          path: "artifact.heartwood-test",
          status: "added",
          source: "session-action",
          action_ids: ["action-5"],
        },
      ],
    };
    const second: WorkspaceDiff = {
      schema_version: "heartwood.workspace-diff.v1",
      path: "second.py",
      status: "non-git",
      source: "session-action",
      original: "before = True\n",
      modified: null,
      truncated: false,
      message: "Only the recorded session action is available.",
    };
    const client = clientWith({
      getWorkspaceChanges: vi.fn().mockResolvedValue(changes),
      getWorkspaceDiff: vi.fn(
        (_sessionId: string, path: string): Promise<WorkspaceDiff> => {
          if (path === "first.py") return first;
          if (path === "third.py")
            return Promise.reject(new Error("Diff failed"));
          if (path === "unsupported.bin") {
            return Promise.resolve({
              ...second,
              path,
              status: "unsupported",
              original: null,
              modified: null,
              message: null,
            });
          }
          if (path === "artifact.heartwood-test") {
            return Promise.resolve({
              ...second,
              path,
              status: "available",
              original: null,
              modified: "synthetic result\n",
              message: null,
            });
          }
          return Promise.resolve(second);
        },
      ),
    });

    render(
      <ProjectWorkspace
        client={client}
        mode="changes"
        revision={1}
        sessionId="synthetic"
      />,
    );

    const workspace = await screen.findByRole("region", {
      name: "Project changes",
    });
    expect(within(workspace).getByText("Session actions")).toBeInTheDocument();
    fireEvent.click(
      within(workspace).getByRole("button", {
        name: "first.py, Added",
      }),
    );
    fireEvent.click(
      within(workspace).getByRole("button", {
        name: "second.py, Deleted",
      }),
    );
    const changePreview = await screen.findByRole("region", {
      name: "Read-only change: second.py",
    });
    expect(changePreview).toHaveTextContent("before = True");
    expect(
      within(changePreview).getByRole("textbox", {
        name: "Original file contents: second.py",
      }),
    ).toHaveAttribute("aria-readonly", "true");
    expect(
      within(changePreview).getByRole("textbox", {
        name: "Modified file contents: second.py",
      }),
    ).toHaveAttribute("aria-readonly", "true");
    expect(
      within(changePreview).getByLabelText(
        "Scrollable original file contents: second.py",
      ),
    ).toHaveAttribute("tabindex", "0");
    expect(
      within(changePreview).getByLabelText(
        "Scrollable modified file contents: second.py",
      ),
    ).toHaveAttribute("tabindex", "0");

    await act(async () => {
      resolveFirst?.({
        ...second,
        path: "first.py",
        status: "available",
        original: "",
        modified: "stale = True\n",
        message: null,
      });
      await first;
    });
    expect(
      screen.getByRole("region", {
        name: "Read-only change: second.py",
      }),
    ).not.toHaveTextContent("stale = True");

    fireEvent.click(
      within(workspace).getByRole("button", {
        name: "third.py, Modified",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Diff failed");
    expect(screen.queryByText("Loading third.py")).not.toBeInTheDocument();

    fireEvent.click(
      within(workspace).getByRole("button", {
        name: "unsupported.bin, Modified",
      }),
    );
    expect(
      await screen.findByText("This unsupported change cannot be displayed."),
    ).toBeInTheDocument();

    fireEvent.click(
      within(workspace).getByRole("button", {
        name: "artifact.heartwood-test, Added",
      }),
    );
    const artifactPreview = await screen.findByRole("region", {
      name: "Read-only change: artifact.heartwood-test",
    });
    await waitFor(() => {
      expect(artifactPreview).toHaveTextContent("synthetic result");
    });
  });

  it("explains empty and unavailable workspace states", async () => {
    const client = clientWith({
      getWorkspaceChanges: vi.fn().mockResolvedValue({
        ...emptyChanges(),
        source: "unavailable",
        status: "unavailable",
      }),
    });

    render(
      <ProjectWorkspace
        client={client}
        mode="changes"
        revision={1}
        sessionId="synthetic"
      />,
    );

    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("No changes are available.")).toBeInTheDocument();
    expect(
      screen.getByText("Select a changed file to inspect it."),
    ).toBeInTheDocument();
  });

  it("refreshes a selected file when the session revision advances", async () => {
    let content = "answer = 1\n";
    const tree: WorkspaceTree = {
      ...emptyTree(),
      entries: [
        {
          path: "analysis.py",
          name: "analysis.py",
          kind: "file",
          depth: 1,
          size_bytes: 11,
        },
      ],
    };
    const getWorkspaceFile = vi.fn(
      (_sessionId: string, path: string): Promise<WorkspaceFile> =>
        Promise.resolve({
          schema_version: "heartwood.workspace-file.v1",
          path,
          status: "available",
          content,
          size_bytes: content.length,
          bytes_read: content.length,
          line_count: 1,
          truncated: false,
          message: null,
        }),
    );
    const client = clientWith({
      getWorkspaceTree: vi.fn().mockResolvedValue(tree),
      getWorkspaceFile,
    });
    const { rerender } = render(
      <ProjectWorkspace
        client={client}
        mode="files"
        revision={1}
        sessionId="synthetic"
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "analysis.py" }));
    await waitFor(() =>
      expect(
        screen.getByRole("region", { name: "Read-only file: analysis.py" }),
      ).toHaveTextContent("answer = 1"),
    );

    content = "answer = 2\n";
    rerender(
      <ProjectWorkspace
        client={client}
        mode="files"
        revision={2}
        sessionId="synthetic"
      />,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("region", { name: "Read-only file: analysis.py" }),
      ).toHaveTextContent("answer = 2"),
    );
    expect(getWorkspaceFile).toHaveBeenCalledTimes(2);
  });

  it("contains overview and file failures without losing the project list", async () => {
    const overviewClient = clientWith({
      getWorkspaceTree: vi.fn().mockRejectedValue("unavailable"),
    });
    const { unmount } = render(
      <ProjectWorkspace
        client={overviewClient}
        mode="files"
        revision={1}
        sessionId="synthetic"
      />,
    );
    expect(
      await screen.findByText("Workspace inspection failed."),
    ).toBeInTheDocument();
    unmount();

    const fileClient = clientWith({
      getWorkspaceTree: vi.fn().mockResolvedValue({
        ...emptyTree(),
        entries: [
          {
            path: "binary.dat",
            name: "binary.dat",
            kind: "file",
            depth: 1,
            size_bytes: 8,
          },
          {
            path: "failed.txt",
            name: "failed.txt",
            kind: "file",
            depth: 1,
            size_bytes: 8,
          },
        ],
      }),
      getWorkspaceFile: vi.fn(
        (_sessionId: string, path: string): Promise<WorkspaceFile> =>
          path === "failed.txt" ?
            Promise.reject(new Error("File request failed"))
          : Promise.resolve({
              schema_version: "heartwood.workspace-file.v1",
              path: "binary.dat",
              status: "binary",
              content: null,
              size_bytes: 8,
              bytes_read: 8,
              line_count: 0,
              truncated: false,
              message: null,
            }),
      ),
    });
    render(
      <ProjectWorkspace
        client={fileClient}
        mode="files"
        revision={1}
        sessionId="synthetic"
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "binary.dat" }));
    expect(
      await screen.findByText("This binary file cannot be displayed."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "binary.dat" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "failed.txt" }));
    expect(await screen.findByText("File request failed")).toBeInTheDocument();
    expect(screen.queryByText("Loading failed.txt")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "failed.txt" })).toBeVisible();
  });

  it("resets selection when an instance is repurposed for another workspace mode", async () => {
    const tree: WorkspaceTree = {
      ...emptyTree(),
      entries: [
        {
          path: "source.py",
          name: "source.py",
          kind: "file",
          depth: 1,
          size_bytes: 12,
        },
      ],
    };
    const changes: WorkspaceChanges = {
      ...emptyChanges(),
      changes: [
        {
          path: "result.py",
          status: "added",
          source: "git",
          action_ids: [],
        },
      ],
    };
    const client = clientWith({
      getWorkspaceTree: vi.fn().mockResolvedValue(tree),
      getWorkspaceFile: vi.fn().mockResolvedValue({
        schema_version: "heartwood.workspace-file.v1",
        path: "source.py",
        status: "available",
        content: "source = 1\n",
        size_bytes: 11,
        bytes_read: 11,
        line_count: 1,
        truncated: false,
        message: null,
      }),
      getWorkspaceChanges: vi.fn().mockResolvedValue(changes),
      getWorkspaceDiff: vi.fn().mockResolvedValue({
        schema_version: "heartwood.workspace-diff.v1",
        path: "result.py",
        status: "available",
        source: "git",
        original: "",
        modified: "result = 1\n",
        truncated: false,
        message: null,
      }),
    });
    const { rerender } = render(
      <ProjectWorkspace
        client={client}
        mode="files"
        revision={1}
        sessionId="synthetic"
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "source.py" }));
    await screen.findByRole("region", { name: "Read-only file: source.py" });
    rerender(
      <ProjectWorkspace
        client={client}
        mode="changes"
        revision={1}
        sessionId="synthetic"
      />,
    );

    expect(
      await screen.findByText("Select a changed file to inspect it."),
    ).toBeInTheDocument();
    fireEvent.click(
      await screen.findByRole("button", { name: "result.py, Added" }),
    );
    await screen.findByRole("region", { name: "Read-only change: result.py" });
    rerender(
      <ProjectWorkspace
        client={client}
        mode="files"
        revision={1}
        sessionId="synthetic"
      />,
    );

    expect(
      await screen.findByText("Select a text file to inspect it."),
    ).toBeInTheDocument();
  });
});
