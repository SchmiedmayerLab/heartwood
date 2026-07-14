/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { expect, test, type Page, type Route } from "@playwright/test";
import { event, syntheticEvents } from "../test/fixtures";
import type { SessionSummary } from "../types";

test.beforeEach(async ({ page }) => installGatewayRoutes(page));

test("supports the researcher conversation and session workflow", async ({
  page,
}) => {
  await page.goto("/");

  await expect(page.getByText("Heartwood", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Set up Heartwood" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  const newAnalysis = page.getByRole("button", { name: "New analysis" });
  await expect(newAnalysis).toHaveCSS("display", "flex");
  await expect(newAnalysis).toHaveCSS("gap", "8px");
  await expect(newAnalysis).toHaveCSS("border-top-width", "1px");
  await expect(
    page.getByRole("heading", { name: "Synthetic cohort analysis" }),
  ).toBeVisible();
  await expect(
    page.getByRole("log", { name: "Conversation transcript" }),
  ).toBeVisible();
  const task = page.getByRole("textbox", { name: "Task", exact: true });
  await expect(task).toBeDisabled();
  await expect(page.getByLabel("Pause agent")).toBeDisabled();
  await expect(page.getByText("Setup needed", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Boundary evidence", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Workflow progress", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("I will run the repository-verified cohort Skill."),
  ).toBeVisible();
  await expect(page.getByText("generic", { exact: true })).toBeVisible();
  await expect(page.getByText("omop-cdm", { exact: true })).toBeVisible();

  const approval = page.getByRole("region", {
    name: "Approval required for OpenHands action set",
  });
  await expect(
    approval.getByText(
      "OpenHands proposed these actions together. One decision applies to every action below.",
    ),
  ).toBeVisible();
  await expect(approval.getByText("low risk")).toBeVisible();
  await expect(
    approval.getByText("build the aggregate synthetic target-condition cohort"),
  ).toBeVisible();
  const allowActions = page.getByLabel("Allow all 1 action once");
  await expect(allowActions).toBeVisible();
  await allowActions.click();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export audit" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("session-test-audit.jsonl");

  await page.getByRole("button", { name: "Skills" }).click();
  await expect(page.getByRole("heading", { name: "Skills" })).toBeVisible();
  await expect(page.getByText("omop-cohort-summary")).toBeVisible();
  await expect(page.getByText("baseline-model")).toBeVisible();
  await expect(page.getByText("aggregate-export")).toBeVisible();
  await page.getByRole("button", { name: "Close" }).click();

  const modelPolicyButton = page.getByRole("button", {
    name: "Settings",
    exact: true,
  });
  await modelPolicyButton.click();
  await expect(
    page.getByRole("heading", { name: "Set up Heartwood" }),
  ).toBeVisible();
  const localConnection = page.locator(".connection-row").filter({
    has: page.getByText("Local", { exact: true }),
  });
  await localConnection.getByRole("button", { name: "Choose" }).click();
  await expect(
    page.locator(".connection-row.selected + .connection-form"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Load models" }).click();
  const modelPicker = page.getByLabel("Models available from Local");
  await expect(modelPicker).toContainText("Local Model");
  await modelPicker.click();
  await page.getByPlaceholder("Search models").fill("Local");
  await expect(
    page.getByRole("option", { name: "Local Model - local-model" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Use model" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(
    page.getByLabel("Active model profile", { exact: true }),
  ).toContainText("Local · local-model");
  await expect(page.getByText("Authorized", { exact: true })).toBeVisible();
  const approvalsTab = page.getByRole("tab", { name: "Approvals" });
  const modelsTab = page.getByRole("tab", { name: "Models" });
  await approvalsTab.click();
  await expect(
    page.getByRole("button", { name: "Ask Every Time" }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("button", { name: "Auto-Approve Low Risk" }),
  ).toBeVisible();
  await approvalsTab.press("ArrowLeft");
  await expect(modelsTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("No recommended models available")).toBeVisible();
  await page.getByText("Other model", { exact: true }).click();
  await page.getByLabel("Model repository").fill("example/research-model-gguf");
  await page.getByRole("button", { name: "Check model" }).click();
  await expect(page.getByText("Research Model Q4_K_M")).toBeVisible();
  await expect(page.getByText(/balanced single-file GGUF/u)).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeHidden();
  await expect(modelPolicyButton).toBeFocused();
  await expect(task).toBeEnabled();

  await task.fill("Fit a training-only age-only condition-history baseline");
  const activity = page.getByRole("status", {
    name: "Heartwood is working on your task",
  });
  await Promise.all([
    page.getByLabel("Send task").click(),
    expect(activity).toBeVisible(),
    expect(task).toBeDisabled(),
  ]);
  await expect(activity).toBeHidden();
  await expect(
    page.getByText("Fit a training-only age-only condition-history baseline"),
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
    page.getByRole("heading", { name: "Set up Heartwood" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("heading", { name: "Synthetic cohort analysis" }),
  ).toBeVisible();
  await page.getByLabel("Open sessions").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: /Synthetic cohort analysis, Approval needed/u,
    }),
  ).toBeVisible();
  await expect(page.getByText("Ask Every Time", { exact: true })).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
});

const installGatewayRoutes = async (page: Page): Promise<void> => {
  let sessionEvents = syntheticEvents();
  let sessions: SessionSummary[] = [
    summary("session-test", "Synthetic cohort analysis", 7),
  ];
  let modelSettings = {
    schema_version: "heartwood.model-settings.v1",
    active_profile: null as string | null,
    model_source: "local",
    profiles: [] as Array<Record<string, unknown>>,
    connections: [
      {
        connection_id: "local",
        label: "Local",
        protocol: "openai-compatible",
        model_prefix: "openai/",
        source: "built-in",
        credential_kind: "none",
        policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
        catalog_endpoint: "http://127.0.0.1:8765/v1/models",
        base_url: "http://127.0.0.1:8765/v1",
        api_key_env: null,
        api_key_file: null,
        api_version: null,
        aws_region_name: null,
        aws_profile_name: null,
        description: "Models installed on this device",
        static_models: [],
        accepts_token: false,
        credential_status: "configured",
      },
    ],
    presets: [
      {
        preset_id: "local-openai-compatible",
        label: "Local OpenAI-Compatible",
        model_prefix: "openai/",
        credential_kind: "none",
        api_key_env: null,
        base_url: "http://127.0.0.1:8765/v1",
        policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
        description: "Local runtime",
      },
    ],
    source_options: [
      {
        source_id: "local",
        connection_id: "local",
        label: "Local",
        description: "Use a model service running with this project.",
        selected: true,
      },
      {
        source_id: "openai",
        connection_id: "openai",
        label: "OpenAI",
        description: "Use the models available to an OpenAI token.",
        selected: false,
      },
      {
        source_id: "anthropic",
        connection_id: "anthropic",
        label: "Anthropic",
        description: "Use the models available to an Anthropic token.",
        selected: false,
      },
      {
        source_id: "stanford-ai-api-gateway",
        connection_id: "stanford-ai-api-gateway",
        label: "Stanford AI API Gateway",
        description:
          "Use models authorized through Stanford's managed gateway.",
        selected: false,
      },
    ],
  };

  await page.route("**/project/readiness", (route) =>
    json(route, {
      state: modelSettings.active_profile ? "ready" : "setup-required",
      platform_id: "generic",
      project_root: "/workspace/synthetic-analysis",
      state_root: "/workspace/synthetic-analysis/.heartwood",
      evidence: [],
      checks: [
        {
          check_id: "project-storage",
          status: "pass",
          summary: "Project storage is writable",
        },
        {
          check_id: "model",
          status: modelSettings.active_profile ? "pass" : "warning",
          summary:
            modelSettings.active_profile ?
              "Active model: local"
            : "No active model selected",
        },
      ],
    }),
  );

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
    const sessionsIndex = parts.lastIndexOf("sessions");
    const sessionId = decodeURIComponent(parts[sessionsIndex + 1] ?? "");
    const resource = parts[sessionsIndex + 2];
    if (resource === "events") {
      await json(route, {
        events: sessionId === "session-test" ? sessionEvents : [],
      });
      return;
    }
    if (resource === "commands") {
      const payload = request.postDataJSON() as { kind?: string };
      if (payload.kind === "chat") {
        await new Promise((resolve) => setTimeout(resolve, 1_500));
      }
      const nextEvents =
        sessionId === "session-test" && payload.kind === "approve" ?
          [
            event(sessionEvents.length, "confirmation.resolved", {
              decision: "approved",
              tool_call_id: "session-test-toolcall-0",
            }),
          ]
        : [];
      sessionEvents = [...sessionEvents, ...nextEvents];
      await json(route, { events: nextEvents });
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
      snapshot_schema_version: "heartwood.model-snapshot-catalog.v1",
      artifacts: [],
      snapshots: [],
      models: [],
      downloads: [],
    }),
  );
  await page.route("**/settings/models/source", async (route) => {
    const payload = route.request().postDataJSON() as { source_id: string };
    const sourceChanged = modelSettings.model_source !== payload.source_id;
    modelSettings = {
      ...modelSettings,
      active_profile: sourceChanged ? null : modelSettings.active_profile,
      model_source: payload.source_id,
      source_options: modelSettings.source_options.map((source) => ({
        ...source,
        selected: source.source_id === payload.source_id,
      })),
    };
    await json(route, modelSettings);
  });
  await page.route("**/settings/models/repository", (route) =>
    json(route, {
      model: {
        model_id: "hf-research-model-123456789abc",
        label: "Research Model Q4_K_M",
        purpose: "User-selected Hugging Face model.",
        runtime: "llama-cpp",
        source_repository: "example/research-model-gguf",
        source_revision: "1".repeat(40),
        source_path: "research-model-q4_k_m.gguf",
        size_bytes: 4 * 1024 * 1024 * 1024,
        minimum_free_bytes: 4 * 1024 * 1024 * 1024,
        license_posture: "Source model card reports apache-2.0.",
        catalog_source: "user-selected",
        artifact_sha256: "a".repeat(64),
        minimum_resource_envelope:
          "Estimated minimum: 4 CPU cores and 12 GB RAM.",
        recommended_resource_envelope:
          "Recommended: 8 CPU cores and 16 GB RAM.",
        available: true,
        availability_reason: "Available on this deployment",
      },
      selection_reason:
        "Selected a balanced single-file GGUF variant for the CPU runtime.",
    }),
  );
  await page.route("**/settings/models/catalog", (route) =>
    json(route, {
      schema_version: "heartwood.model-catalog.v1",
      connection: modelSettings.connections[0],
      models: [
        {
          model_id: "local-model",
          display_name: "Local Model",
          execution_model: "openai/local-model",
          availability: "available",
          reason: "Verified by the pinned OpenHands SDK",
          context_window: 32_768,
          supports_tools: true,
        },
      ],
      refreshed_at: 1_783_683_200,
    }),
  );
  await page.route("**/settings/models/connect", async (route) => {
    const payload = route.request().postDataJSON() as {
      connection_id: string;
      model_id: string;
    };
    const profile = {
      profile_id: payload.connection_id,
      model: `openai/${payload.model_id}`,
      policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
      capability_tier: "supervised",
      base_url: "http://127.0.0.1:8765/v1",
      credential_kind: "none",
      api_key_env: null,
      api_key_file: null,
      api_version: null,
      aws_region_name: null,
      aws_profile_name: null,
      description: "Local runtime",
      credential_status: "configured",
    };
    modelSettings = {
      ...modelSettings,
      active_profile: payload.connection_id,
      model_source: payload.connection_id,
      profiles: [profile],
    };
    await json(route, modelSettings);
  });
  await page.route("**/settings/models/validation**", (route) =>
    json(route, {
      profile: modelSettings.profiles[0],
      credential_status: "configured",
      action_confirmation_mode: "always-confirm",
      policy_decision: {
        decision: "allow",
        endpoint: "http://127.0.0.1:8765/v1/chat/completions",
        reason: "Local route allowed",
      },
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
  await page.route("**/settings/models", (route) => json(route, modelSettings));
  await page.route("**/settings/skills", (route) =>
    json(route, {
      skills: [
        {
          name: "omop-cohort-summary",
          skill_id: "heartwood.synthetic.omop-cohort-summary",
          description: "Target-condition cohort and aggregate quality checks",
          trust_tier: "verified",
          source: "bundled",
          approval_summary: "Reads localized synthetic OMOP tables.",
          declared_tools: ["read-synthetic-tables", "write-aggregate-json"],
          requires_network: false,
        },
        {
          name: "baseline-model",
          skill_id: "heartwood.synthetic.baseline-model",
          description: "Training-only age baseline with aggregate diagnostics",
          trust_tier: "verified",
          source: "bundled",
          approval_summary:
            "Reads synthetic tables and writes model diagnostics.",
          declared_tools: ["read-synthetic-tables", "write-aggregate-json"],
          requires_network: false,
        },
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
