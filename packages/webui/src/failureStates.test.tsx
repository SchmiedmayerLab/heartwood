/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../../../fixtures/synthetic/platform-contract/failure-states.json";
import { GatewayClient } from "./client";
import { ConversationWorkspace } from "./components/ConversationWorkspace";
import { emptyProjection } from "./test/fixtures";

// The terminal and notebook tests in the gateway package render the same fixture.
const states = fixture.states;

describe("platform contract failure states in the browser", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
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
});
