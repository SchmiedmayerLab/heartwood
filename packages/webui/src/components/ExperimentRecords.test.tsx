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
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HeartwoodClient } from "../client";
import type { ExperimentCollection, ExperimentRun } from "../types";
import { ProjectExperimentRecords } from "./ExperimentRecords";

const scriptRun = (): ExperimentRun => ({
  schema_version: "heartwood.experiment-run.v1",
  run_id: "3064c9dc-826f-4793-9203-e381dbf26303",
  status: "succeeded",
  attempt: 1,
  started_at: "2026-09-11T00:00:00Z",
  updated_at: "2026-09-11T00:00:01Z",
  exit_code: 0,
  evidence: [],
  outputs: [{ path: "results.json", size_bytes: 12, sha256: "e".repeat(64) }],
  definition: {
    actor_ref: "researcher",
    source: "shell",
    entry_point: "analysis.py",
    code: [{ path: "analysis.py", sha256: "a".repeat(64), size_bytes: 100 }],
    inputs: [{ path: "data.csv", sha256: "b".repeat(64), size_bytes: 42 }],
    output_paths: ["results.json"],
    code_output_paths: [],
    environment: { kind: "python", source: "observed", sha256: "c".repeat(64) },
    parameters_sha256: "d".repeat(64),
    invocation_sha256: "f".repeat(64),
    git_revision: null,
    git_dirty: null,
    stage: null,
  },
});
const collection = (
  runs: ExperimentRun[] = [scriptRun()],
): ExperimentCollection => ({
  schema_version: "heartwood.experiment-collection.v1",
  retention: "project-local",
  runs,
});
const client = () => ({
  getExperimentRecords: vi
    .fn<HeartwoodClient["getExperimentRecords"]>()
    .mockResolvedValue(collection()),
  getExperimentExport: vi
    .fn<HeartwoodClient["getExperimentExport"]>()
    .mockResolvedValue({
      schema_version: "heartwood.experiment-export.v1",
      sha256: "a".repeat(64),
      jsonl: '{"synthetic":true}\n',
    }),
});
const open = () =>
  fireEvent.click(screen.getByText("Project Experiment Records"));

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("project experiment records", () => {
  it("loads on request and displays script provenance without a workflow session", async () => {
    const api = client();
    render(<ProjectExperimentRecords client={api} revision="session:0" />);
    expect(api.getExperimentRecords).not.toHaveBeenCalled();
    open();
    expect(
      await screen.findByRole("heading", { name: "analysis.py" }),
    ).toBeVisible();
    expect(screen.getByText("e".repeat(64))).toBeVisible();
    fireEvent.click(screen.getByText("Inputs and Code"));
    expect(screen.getByText("data.csv")).toBeVisible();
    expect(screen.getByText("b".repeat(64))).toBeVisible();
    expect(api.getExperimentExport).not.toHaveBeenCalled();
  });

  it("clears superseded records and ignores late responses from another project", async () => {
    let resolve!: (value: ExperimentCollection) => void;
    const old = client();
    old.getExperimentRecords.mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const view = render(
      <ProjectExperimentRecords client={old} revision="old:1" />,
    );
    open();
    await waitFor(() =>
      expect(old.getExperimentRecords).toHaveBeenCalledOnce(),
    );
    const next = client();
    next.getExperimentRecords.mockResolvedValue(collection([]));
    view.rerender(<ProjectExperimentRecords client={next} revision="new:1" />);
    expect(
      await screen.findByText("No experiments recorded in this project."),
    ).toBeVisible();
    await act(async () => {
      resolve(collection());
      await Promise.resolve();
    });
    expect(
      screen.queryByRole("heading", { name: "analysis.py" }),
    ).not.toBeInTheDocument();
  });

  it("refreshes after revision changes and handles a failed refresh without stale results", async () => {
    const api = client();
    const view = render(
      <ProjectExperimentRecords client={api} revision="session:1" />,
    );
    open();
    await screen.findByRole("heading", { name: "analysis.py" });
    api.getExperimentRecords.mockRejectedValueOnce(
      new Error("Journal needs integrity recovery"),
    );
    view.rerender(
      <ProjectExperimentRecords client={api} revision="session:2" />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Journal needs integrity recovery",
    );
    expect(
      screen.queryByRole("heading", { name: "analysis.py" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(
      await screen.findByRole("heading", { name: "analysis.py" }),
    ).toBeVisible();
  });

  it("downloads exact gateway JSONL only on explicit export and releases the object URL", async () => {
    const api = client();
    const create = vi.fn().mockReturnValue("blob:synthetic-export");
    const revoke = vi.fn();
    vi.stubGlobal(
      "URL",
      Object.assign(class extends URL {}, {
        createObjectURL: create,
        revokeObjectURL: revoke,
      }),
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    render(<ProjectExperimentRecords client={api} revision="session:1" />);
    open();
    await screen.findByRole("heading", { name: "analysis.py" });
    fireEvent.click(screen.getByRole("button", { name: "Export Records" }));
    await waitFor(() => expect(click).toHaveBeenCalledOnce());
    const blob = create.mock.calls[0]?.[0] as Blob;
    expect(blob.type).toBe("application/x-ndjson");
    expect(blob.size).toBe(
      new TextEncoder().encode('{"synthetic":true}\n').length,
    );
    const text = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("Synthetic export read failed"));
      reader.readAsText(blob);
    });
    expect(text).toBe('{"synthetic":true}\n');
    expect(click.mock.instances[0]).toHaveAttribute(
      "download",
      "heartwood-experiments-aaaaaaaaaaaa.jsonl",
    );
    expect(revoke).toHaveBeenCalledWith("blob:synthetic-export");
    expect(screen.getByRole("status")).toHaveTextContent("a".repeat(64));
    vi.unstubAllGlobals();
  });

  it("does not report export success when downloading fails", async () => {
    const api = client();
    api.getExperimentExport.mockRejectedValue(new Error("Export unavailable"));
    render(<ProjectExperimentRecords client={api} revision="session:1" />);
    open();
    await screen.findByRole("heading", { name: "analysis.py" });
    fireEvent.click(screen.getByRole("button", { name: "Export Records" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Export unavailable",
    );
    expect(screen.queryByText(/Export SHA-256/)).not.toBeInTheDocument();
  });

  it("reports a browser download failure instead of claiming a successful export", async () => {
    vi.stubGlobal("URL", { createObjectURL: undefined });
    render(<ProjectExperimentRecords client={client()} revision="session:1" />);
    open();
    await screen.findByRole("heading", { name: "analysis.py" });
    fireEvent.click(screen.getByRole("button", { name: "Export Records" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This browser cannot download the export",
    );
    expect(screen.queryByText(/Export SHA-256/)).not.toBeInTheDocument();
  });

  it("does not download a late export after leaving the project", async () => {
    const api = client();
    let resolve!: (
      value: Awaited<ReturnType<HeartwoodClient["getExperimentExport"]>>,
    ) => void;
    api.getExperimentExport.mockReturnValue(
      new Promise((done) => {
        resolve = done;
      }),
    );
    const view = render(
      <ProjectExperimentRecords client={api} revision="session:1" />,
    );
    open();
    await screen.findByRole("heading", { name: "analysis.py" });
    fireEvent.click(screen.getByRole("button", { name: "Export Records" }));
    view.unmount();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click");
    await act(async () => {
      resolve({
        schema_version: "heartwood.experiment-export.v1",
        sha256: "a".repeat(64),
        jsonl: "",
      });
      await Promise.resolve();
    });
    expect(click).not.toHaveBeenCalled();
  });
});
