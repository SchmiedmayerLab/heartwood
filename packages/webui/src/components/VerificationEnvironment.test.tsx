/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { downloadTextFile } from "../downloads";
import type { PythonEnvironmentSnapshot } from "../types";
import { VerificationEnvironment } from "./VerificationEnvironment";

vi.mock("../downloads", () => ({ downloadTextFile: vi.fn() }));
const snapshot: PythonEnvironmentSnapshot = {
  schema_version: "heartwood.python-environment.v1",
  implementation: "CPython",
  python: "3.12.13",
  system: "Linux",
  machine: "x86_64",
  packages: [["example-package", "1.2"]],
};

describe("verification environment capture", () => {
  beforeEach(() => vi.mocked(downloadTextFile).mockReset());

  it("captures only on request and downloads the gateway snapshot", async () => {
    const client = {
      getVerificationEnvironment: vi.fn().mockResolvedValue(snapshot),
    };
    render(<VerificationEnvironment client={client} />);
    expect(client.getVerificationEnvironment).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() =>
      expect(downloadTextFile).toHaveBeenCalledWith(
        "python-environment.json",
        JSON.stringify(snapshot, null, 2) + "\n",
      ),
    );
    expect(client.getVerificationEnvironment).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button")).toBeEnabled();
  });

  it("shows pending state and discards a result after leaving the interface", async () => {
    let resolve!: (value: PythonEnvironmentSnapshot) => void;
    const client = {
      getVerificationEnvironment: vi.fn().mockReturnValue(
        new Promise<PythonEnvironmentSnapshot>((done) => {
          resolve = done;
        }),
      ),
    };
    const view = render(<VerificationEnvironment client={client} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("button")).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Inspecting Python environment",
    );
    view.unmount();
    resolve(snapshot);
    await Promise.resolve();
    expect(downloadTextFile).not.toHaveBeenCalled();
  });

  it("reports failures and allows retry without claiming a file was captured", async () => {
    const client = {
      getVerificationEnvironment: vi
        .fn()
        .mockRejectedValue(new Error("Python unavailable")),
    };
    render(<VerificationEnvironment client={client} />);
    fireEvent.click(screen.getByRole("button"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Python unavailable",
    );
    expect(downloadTextFile).not.toHaveBeenCalled();
    expect(screen.getByRole("button")).toBeEnabled();
    client.getVerificationEnvironment.mockResolvedValue(snapshot);
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(downloadTextFile).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
