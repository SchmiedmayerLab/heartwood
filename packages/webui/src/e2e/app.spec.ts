/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { expect, test } from "@playwright/test";
import { syntheticEvents } from "../test/fixtures";

test("loads the web UI and renders mocked gateway events", async ({ page }) => {
  await page.route("**/sessions/**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      status: 200,
      body: JSON.stringify({ events: syntheticEvents() }),
    });
  });
  await page.route("**/settings/models/artifacts", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      status: 200,
      body: JSON.stringify({
        schema_version: "heartwood.local-model-catalog.v1",
        artifacts: [],
        downloads: [],
      }),
    });
  });
  await page.route("**/settings/actions", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      status: 200,
      body: JSON.stringify({
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
    });
  });
  await page.route("**/settings/models", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      status: 200,
      body: JSON.stringify({
        schema_version: "heartwood.model-settings.v1",
        active_profile: null,
        profiles: [],
        presets: [],
      }),
    });
  });
  await page.route("**/settings/skills", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      status: 200,
      body: JSON.stringify({
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
    });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Heartwood" })).toBeVisible();
  await expect(
    page.getByRole("log", { name: "Conversation transcript" }),
  ).toBeVisible();
  await expect(
    page.getByText("I will inspect the synthetic workspace."),
  ).toBeVisible();
  await expect(page.getByLabel("Allow session-test-toolcall-0")).toBeVisible();

  await page.getByLabel("Skills").click();
  await expect(page.getByRole("heading", { name: "Skills" })).toBeVisible();
  await expect(page.getByText("aggregate-export")).toBeVisible();

  await page.getByLabel("Settings").click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Ask Every Time" }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("button", { name: "Auto-Approve Low Risk" }),
  ).toBeVisible();
});
