/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GatewayClient } from "./client";
import { ConversationWorkspace } from "./components/ConversationWorkspace";
import { emptyProjection } from "./test/fixtures";

interface FailureState {
  condition: string;
  code: string;
  surface: string;
  diagnostic: string;
}

// The terminal and notebook tests in the gateway package render the same fixture.
// It is read at run time because the image build type-checks this package without it.
const { states } = JSON.parse(
  readFileSync(
    resolve(
      import.meta.dirname,
      "../../../fixtures/synthetic/platform-contract/failure-states.json",
    ),
    "utf8",
  ),
) as { states: FailureState[] };

describe("platform contract failure states in the browser", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reads every surface from the shared fixture", () => {
    expect(new Set(states.map((state) => state.surface))).toEqual(
      new Set([
        "gateway-response",
        "model-discovery",
        "session",
        "setup-refusal",
      ]),
    );
  });

  it.each(states.filter((state) => state.surface === "session"))(
    "shows $code from the session projection",
    ({ diagnostic }) => {
      render(
        <ConversationWorkspace
          conversationEndRef={createRef<HTMLDivElement>()}
          actionPresentation={null}
          modelConfigured
          modelMessage=""
          projection={{
            ...emptyProjection(),
            conversation: [
              {
                id: "session-test-event-000004-error",
                sequence: 4,
                role: "trace",
                label: "System",
                content: "The task could not be completed",
                detail: diagnostic,
                technicalDetail: null,
              },
            ],
          }}
          prompt=""
          requestActivity={null}
          requestStatus="idle"
          onDecision={vi.fn()}
          onRetryApproval={null}
          onOpenSettings={vi.fn()}
          onPauseToggle={vi.fn()}
          onPrompt={vi.fn()}
          onSubmit={vi.fn()}
        />,
      );

      expect(screen.getByText(diagnostic)).toBeInTheDocument();
    },
  );

  it.each(states.filter((state) => state.surface === "gateway-response"))(
    "reports $code from a gateway refusal",
    async ({ code, diagnostic }) => {
      const error = diagnostic.slice(code.length + 2);
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue(
            new Response(JSON.stringify({ code, error }), { status: 401 }),
          ),
      );

      await expect(
        new GatewayClient("/proxy/8767").replayEvents("session-test"),
      ).rejects.toThrow(diagnostic);
    },
  );

  it.each(states.filter((state) => state.surface === "setup-refusal"))(
    "reports $code when a choice is not offered",
    async ({ code, diagnostic }) => {
      const error = diagnostic.slice(code.length + 2);
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue(
            new Response(JSON.stringify({ code, error }), { status: 422 }),
          ),
      );

      await expect(
        new GatewayClient("/proxy/8767").configureModelSource("openai"),
      ).rejects.toThrow(diagnostic);
    },
  );

  it.each(states.filter((state) => state.surface === "model-discovery"))(
    "reports $code from model discovery",
    async ({ code, diagnostic }) => {
      const error = diagnostic.slice(code.length + 2);
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue(
            new Response(JSON.stringify({ code, error }), { status: 422 }),
          ),
      );

      await expect(
        new GatewayClient("/proxy/8767").discoverModels({
          connection_id: "synthetic-platform",
          refresh: true,
        }),
      ).rejects.toThrow(diagnostic);
    },
  );
});
