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

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Heartwood" })).toBeVisible();
  await expect(page.getByText("omop-cdm")).toBeVisible();
  await expect(page.getByText("local-loopback")).toBeVisible();
});
