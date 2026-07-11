/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { syntheticEvents } from "../test/fixtures";
import type { SessionSummary } from "../types";

test.beforeEach(async ({ page }) => installGatewayRoutes(page));

test("supports the researcher conversation and session workflow", async ({
  page,
}) => {
  await page.goto("/");

  await expect(page.getByText("Heartwood", { exact: true })).toBeVisible();
  const newAnalysis = page.getByRole("button", { name: "New analysis" });
  await expect(newAnalysis).toHaveCSS("display", "flex");
  await expect(newAnalysis).toHaveCSS("gap", "8px");
  await expect(newAnalysis).toHaveCSS("border-top-width", "1px");
  await expect(
    page.getByRole("heading", { name: "Synthetic analysis" }),
  ).toBeVisible();
  await expect(
    page.getByRole("log", { name: "Conversation transcript" }),
  ).toBeVisible();
  await expect(
    page.getByText("I will inspect the synthetic workspace."),
  ).toBeVisible();
  await expect(page.getByText("generic", { exact: true })).toBeVisible();
  await expect(page.getByText("omop-cdm", { exact: true })).toBeVisible();

  const approval = page.getByRole("region", {
    name: "Approval required for heartwood.local.write_summary",
  });
  await expect(approval.getByText("low risk")).toBeVisible();
  await expect(
    approval.getByText("write a synthetic workspace summary artifact"),
  ).toBeVisible();
  await expect(page.getByLabel("Allow session-test-toolcall-0")).toBeVisible();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export audit" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("session-test-audit.jsonl");

  await page.getByRole("button", { name: "Skills" }).click();
  await expect(page.getByRole("heading", { name: "Skills" })).toBeVisible();
  await expect(page.getByText("aggregate-export")).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();

  const modelPolicyButton = page.getByRole("button", {
    name: "Model & policy",
  });
  await modelPolicyButton.click();
  await expect(
    page.getByRole("heading", { name: "Model & policy" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Ask Every Time" }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("button", { name: "Auto-Approve Low Risk" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("heading", { name: "Model & policy" }),
  ).toBeHidden();
  await expect(modelPolicyButton).toBeFocused();

  const task = page.getByRole("textbox", { name: "Task", exact: true });
  await task.fill("Summarize the synthetic workspace");
  await task.press("Enter");
  await expect(
    page.getByText("Summarize the synthetic workspace"),
  ).toBeVisible();

  await page.getByRole("button", { name: "New analysis" }).click();
  await expect(
    page.getByRole("heading", { name: "Untitled session" }),
  ).toBeVisible();
  await page.getByLabel("Rename session").click();
  await page.getByLabel("Session title").fill("Reproducible analysis");
  await page.getByLabel("Session title").press("Enter");
  await expect(
    page.getByRole("heading", { name: "Reproducible analysis" }),
  ).toBeVisible();
});

test("keeps session navigation usable on a narrow notebook viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 760 });
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: "Synthetic analysis" }),
  ).toBeVisible();
  await page.getByLabel("Open sessions").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Synthetic analysis/u }),
  ).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
});

const installGatewayRoutes = async (page: Page): Promise<void> => {
  let sessions: SessionSummary[] = [
    summary("session-test", "Synthetic analysis", 7),
  ];

  await page.route("**/sessions", async (route) => {
    if (route.request().method() === "POST") {
      const created = summary("session-created", "Untitled session", 0);
      sessions = [created, ...sessions];
      await json(route, created, 201);
      return;
    }
    await json(route, { sessions });
  });

  await page.route("**/sessions/**", async (route) => {
    const request = route.request();
    const parts = new URL(request.url()).pathname.split("/").filter(Boolean);
    const sessionId = decodeURIComponent(parts[1] ?? "");
    const resource = parts[2];
    if (resource === "events") {
      await json(route, {
        events: sessionId === "session-test" ? syntheticEvents() : [],
      });
      return;
    }
    if (resource === "commands") {
      await json(route, { events: [] });
      return;
    }
    if (resource === "audit-export") {
      await json(route, {
        filename: `${sessionId}-audit.jsonl`,
        content: '{"kind":"audit.export.recorded"}\n',
      });
      return;
    }
    const current = sessions.find(
      (session) => session.session_id === sessionId,
    );
    if (request.method() === "PATCH" && current) {
      const payload = request.postDataJSON() as { title: string };
      const renamed = { ...current, title: payload.title };
      sessions = sessions.map((session) =>
        session.session_id === sessionId ? renamed : session,
      );
      await json(route, renamed);
      return;
    }
    await json(route, current ?? summary(sessionId, sessionId, 0));
  });

  await page.route("**/settings/models/artifacts", (route) =>
    json(route, {
      schema_version: "heartwood.local-model-catalog.v1",
      artifacts: [],
      downloads: [],
    }),
  );
  await page.route("**/settings/actions", (route) =>
    json(route, {
      schema_version: "heartwood.action-settings.v1",
      confirmation_mode: "always-confirm",
      modes: [
        { mode: "always-confirm", label: "Ask Every Time", allowed: true },
        {
          mode: "confirm-risky",
          label: "Auto-Approve Low Risk",
          allowed: true,
        },
      ],
    }),
  );
  await page.route("**/settings/models", (route) =>
    json(route, {
      schema_version: "heartwood.model-settings.v1",
      active_profile: null,
      profiles: [],
      presets: [],
    }),
  );
  await page.route("**/settings/skills", (route) =>
    json(route, {
      skills: [
        {
          name: "aggregate-export",
          skill_id: "heartwood.synthetic.aggregate-export",
          description: "Aggregate export Skill",
          trust_tier: "verified",
          source: "bundled",
          approval_summary: "Writes reviewed aggregate output.",
          declared_tools: ["write-aggregate-json"],
          requires_network: false,
        },
      ],
    }),
  );
};

const json = async (
  route: Route,
  body: unknown,
  status = 200,
): Promise<void> => {
  await route.fulfill({
    body: JSON.stringify(body),
    contentType: "application/json",
    status,
  });
};

const summary = (
  sessionId: string,
  title: string,
  eventCount: number,
): SessionSummary => ({
  session_id: sessionId,
  title,
  status: eventCount === 0 ? "empty" : "waiting",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  event_count: eventCount,
});
