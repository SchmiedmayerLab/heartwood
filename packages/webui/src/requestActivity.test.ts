/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { describe, expect, it } from "vitest";
import { requestActivityForCommand } from "./requestActivity";

describe("requestActivityForCommand", () => {
  it("uses model-aware waiting copy for task and continuation commands", () => {
    expect(requestActivityForCommand("chat")).toEqual(
      requestActivityForCommand("run"),
    );
    expect(requestActivityForCommand("resume").guidance).toContain(
      "selected model",
    );
    expect(requestActivityForCommand("approve").label).toContain(
      "approved action set",
    );
  });

  it("describes utility commands without claiming agent workflow progress", () => {
    expect(requestActivityForCommand("detect").label).toBe(
      "Inspecting the project environment",
    );
    expect(requestActivityForCommand("audit.export").guidance).toContain(
      "session histories",
    );
    expect(requestActivityForCommand("deny").guidance).not.toContain("model");
  });
});
