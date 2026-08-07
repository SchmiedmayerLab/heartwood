/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import AccessibilityScanner from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";
import {
  credentialIsolation,
  emptyProjection,
  event,
  genericCapabilities,
  syntheticEvents,
  syntheticProjection,
} from "../test/fixtures";
import type {
  PlatformCapabilities,
  SessionSummary,
  StartupPlan,
} from "../types";

test.beforeEach(async ({ page }) => installGatewayRoutes(page));

const closeSheet = async (page: Page): Promise<void> => {
  const close = page.getByRole("button", { name: "Close", exact: true });
  await close.click();
  await expect(close).toBeHidden();
};

test("supports the researcher conversation and session workflow", async ({
  page,
}) => {
  await page.goto("/");

  await expect(page.getByText("Heartwood", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Set up Heartwood" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Close", exact: true }),
  ).toBeHidden();
  await page.getByLabel("Open action review settings").click();
  await expect(
    page.getByRole("tab", { name: "Action Review" }),
  ).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Close", exact: true }),
  ).toBeHidden();
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
  const inactiveWorkspacePanels = page.locator(
    '.workspace-tab-panel[data-state="inactive"]',
  );
  await expect(inactiveWorkspacePanels).toHaveCount(2);
  await expect(inactiveWorkspacePanels.first()).toBeHidden();
  await expect(inactiveWorkspacePanels.last()).toBeHidden();
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
  await expect(
    page.getByText("Workstation or container", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Not configured", { exact: true })).toBeVisible();
  await expectNoAccessibilityViolations(page);

  const approval = page.getByRole("region", {
    name: "One Decision for This Action Set",
  });
  await expect(
    approval.getByText(
      "These actions were proposed together. Allowing runs every action once; rejecting runs none of them.",
    ),
  ).toBeVisible();
  await expect(approval.getByText("Low Risk")).toBeVisible();
  await expect(
    approval.getByText("build the aggregate synthetic target-condition cohort"),
  ).toBeVisible();
  const allowActions = page.getByLabel("Allow 1 action once");
  await expect(allowActions).toBeVisible();
  await allowActions.click();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export audit" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("session-test-audit.jsonl");

  await page.getByRole("button", { name: "Specialists" }).click();
  await expect(
    page.getByRole("heading", { name: "Research specialists" }),
  ).toBeVisible();
  await expect(page.getByText("Research Planner")).toBeVisible();
  await page.getByText("Additional roles").click();
  await expect(page.getByText("Analysis Implementer")).toBeVisible();
  await expect(
    page.getByText(
      "Tool-enabled specialists require restart-safe child review.",
    ),
  ).toBeVisible();
  await closeSheet(page);

  await page.getByRole("button", { name: "Skills" }).click();
  await expect(page.getByRole("heading", { name: "Skills" })).toBeVisible();
  await expect(page.getByText("omop-cohort-summary")).toBeVisible();
  await expect(page.getByText("baseline-model")).toBeVisible();
  await expect(page.getByText("aggregate-export")).toBeVisible();
  await closeSheet(page);

  const modelPolicyButton = page.getByRole("button", {
    name: "Settings",
    exact: true,
  });
  await modelPolicyButton.click();
  await expect(
    page.getByRole("heading", { name: "Set up Heartwood" }),
  ).toBeVisible();
  const hostedConnection = page.locator(".connection-row").filter({
    has: page.getByText("OpenAI", { exact: true }),
  });
  await hostedConnection.getByRole("button", { name: "Connect" }).click();
  await page.getByLabel("API key").fill("synthetic-runtime-token");
  const loadModels = page.getByRole("button", { name: "Load models" });
  await expect(loadModels).toBeVisible();
  await loadModels.click();
  const modelPicker = page.getByLabel("Models available from OpenAI");
  await expect(modelPicker).toContainText("Research Coder");
  await modelPicker.click();
  await page.getByPlaceholder("Search models").fill("Research");
  await expect(
    page.getByRole("option", { name: "Research Coder - research-coder" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Use model" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(
    page.getByLabel("Active model profile", { exact: true }),
  ).toContainText("OpenAI · research-coder");
  await expect(page.getByText("Authorized", { exact: true })).toBeVisible();
  const approvalsTab = page.getByRole("tab", { name: "Action Review" });
  const modelsTab = page.getByRole("tab", { name: "Models" });
  await approvalsTab.click();
  await expect(
    page.getByRole("radio", { name: /Review Every Action/u }),
  ).toBeChecked();
  await expect(
    page.getByRole("radio", { name: /Low-Risk Automation/u }),
  ).toBeVisible();
  await approvalsTab.press("ArrowLeft");
  await expect(modelsTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("No recommended models available")).toBeVisible();
  await page.getByText("Other model", { exact: true }).click();
  await page.getByLabel("Model repository").fill("example/research-model-gguf");
  await page.getByText("Version options", { exact: true }).click();
  await page.getByLabel("Model revision").fill("reviewed-release");
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
      name: /Synthetic cohort analysis, Action review needed/u,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Review Every Action", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  const approval = page.getByRole("region", {
    name: "One Decision for This Action Set",
  });
  await expect(approval).toBeVisible();
  await expect(page.getByLabel("Allow 1 action once")).toBeVisible();
  await page.getByLabel("Open action review settings").click();
  await expect(
    page.getByRole("radio", { name: /Review Every Action/u }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  const conversationTab = page.getByRole("tab", { name: "Conversation" });
  await conversationTab.focus();
  await conversationTab.press("ArrowRight");
  const files = page.getByRole("region", { name: "Project files" });
  await expect(files).toBeVisible();
  const fileEntry = files.getByRole("button", { name: "analysis.py" });
  await fileEntry.focus();
  await fileEntry.press("Enter");
  await expect(
    page.getByRole("region", { name: "Read-only file: analysis.py" }),
  ).toContainText("cohort_size = 42");
  const filesTab = page.getByRole("tab", { name: "Files" });
  await filesTab.focus();
  await filesTab.press("ArrowRight");
  const changes = page.getByRole("region", { name: "Project changes" });
  await expect(changes).toBeVisible();
  const changeEntry = changes.getByRole("button", {
    name: "analysis.py, Modified",
  });
  await changeEntry.focus();
  await changeEntry.press("Enter");
  await expect(
    page.getByRole("region", { name: "Read-only change: analysis.py" }),
  ).toContainText("cohort_size = 42");
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  await expectNoAccessibilityViolations(page);
});

test("keeps grouped action review usable on a tablet viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Set up Heartwood" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");

  const approval = page.getByRole("region", {
    name: "One Decision for This Action Set",
  });
  await expect(approval).toBeInViewport({ ratio: 1 });
  await expect(page.getByLabel("Reject 1 action")).toBeVisible();
  await expect(page.getByLabel("Allow 1 action once")).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 768);
  await expectNoAccessibilityViolations(page);
});

test("supports keyboard review, reduced motion, and high-zoom reflow", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/?configured-model=1");
  await page.keyboard.press("Escape");

  const approval = page.getByRole("region", {
    name: "One Decision for This Action Set",
  });
  const heading = approval.getByRole("heading", {
    name: "One Decision for This Action Set",
  });
  await expect(heading).toBeFocused();
  const argumentsDisclosure = approval.getByText("Review Exact Arguments");
  await argumentsDisclosure.focus();
  await page.keyboard.press("Enter");
  await expect(argumentsDisclosure.locator("..")).toHaveAttribute("open", "");

  const reject = page.getByLabel("Reject 1 action");
  await reject.focus();
  await page.keyboard.press("Enter");
  await expect(approval).toBeHidden();
  await expect(
    page.getByRole("status", { name: "Agent status" }),
  ).toContainText("Action Set Rejected");
  await expect(page.getByLabel("Task", { exact: true })).toBeFocused();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 320);

  const animationName = await page.evaluate(() => {
    const marker = document.createElement("span");
    marker.className = "streaming-cursor";
    document.body.append(marker);
    const value = window.getComputedStyle(marker).animationName;
    marker.remove();
    return value;
  });
  expect(animationName).toBe("none");
  await expectNoAccessibilityViolations(page);
});

test("confirms a new project before creating private state", async ({
  page,
}) => {
  await page.goto("/?new-project=1");

  await expect(
    page.getByRole("heading", { name: "Set up Heartwood" }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Review this project before Heartwood creates private project state.",
    ),
  ).toBeVisible();

  await page.getByRole("button", { name: "Use this project" }).click();

  await expect(
    page.getByRole("button", { name: "Use this project" }),
  ).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("heading", { name: "Main session" }),
  ).toBeVisible();
});

const expectNoAccessibilityViolations = async (page: Page): Promise<void> => {
  const results = await new AccessibilityScanner({ page }).analyze();
  expect(results.violations).toEqual([]);
};

const installGatewayRoutes = async (page: Page): Promise<void> => {
  let projectInitialized: boolean | null = null;
  let sessionEvents = syntheticEvents();
  let sessionProjection = syntheticProjection();
  let sessions: SessionSummary[] = [
    summary("session-test", "Synthetic cohort analysis", 7),
  ];
  let modelSettings = {
    schema_version: "heartwood.model-settings.v1",
    active_profile: null as string | null,
    model_source: "heartwood",
    profiles: [] as Array<Record<string, unknown>>,
    credential_store: {
      backends: ["process"],
      default_backend: "process",
      persistence_available: false,
      persistence_description: "Credentials remain in this server process.",
    },
    credential_bindings: [] as Array<Record<string, unknown>>,
    credential_isolation: credentialIsolation(),
    connections: [
      {
        connection_id: "heartwood",
        label: "Run with Heartwood",
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
        description:
          "Models served by the runtime Heartwood manages for this project",
        static_models: [],
        subscription_vendor: null,
        group: "heartwood-managed",
        group_label: "Run with Heartwood",
        accepts_token: false,
        supports_login: false,
        auth_type: "api_key",
        credential_status: "configured",
      },
      {
        connection_id: "openai",
        label: "OpenAI",
        protocol: "openai",
        model_prefix: "openai/",
        source: "built-in",
        credential_kind: "environment",
        policy_endpoint: "https://api.openai.com/v1/chat/completions",
        catalog_endpoint: "https://api.openai.com/v1/models",
        base_url: null,
        api_key_env: "OPENAI_API_KEY",
        api_key_file: null,
        api_version: null,
        aws_region_name: null,
        aws_profile_name: null,
        description: "Models available to the supplied OpenAI credential",
        static_models: [],
        subscription_vendor: null,
        group: "hosted-provider",
        group_label: "Hosted providers",
        accepts_token: true,
        supports_login: false,
        auth_type: "api_key",
        credential_status: "missing",
      },
    ],
    presets: [
      {
        preset_id: "heartwood-managed",
        label: "Heartwood-managed model",
        model_prefix: "openai/",
        credential_kind: "none",
        api_key_env: null,
        base_url: "http://127.0.0.1:8765/v1",
        policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
        description: "Heartwood-managed model",
      },
    ],
    source_options: [
      {
        source_id: "heartwood",
        connection_id: "heartwood",
        label: "Run with Heartwood",
        description: "Use a model service running with this project.",
        selected: true,
      },
      {
        source_id: "openai",
        connection_id: "openai",
        label: "OpenAI",
        description: "Use the models available to an OpenAI API key.",
        selected: false,
      },
      {
        source_id: "anthropic",
        connection_id: "anthropic",
        label: "Anthropic",
        description: "Use the models available to an Anthropic API key.",
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

  const capabilities: PlatformCapabilities = genericCapabilities({
    persistent_storage: "Current project directory",
    credential_backends: ["process", "keyring"],
  });
  const ensureConfiguredModelFromPage = (): void => {
    if (
      modelSettings.active_profile !== null ||
      !new URL(page.url()).searchParams.has("configured-model")
    ) {
      return;
    }
    modelSettings = {
      ...modelSettings,
      active_profile: "heartwood",
      profiles: [
        {
          profile_id: "heartwood",
          model: "openai/synthetic-coder",
          policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
          capability_tier: "supervised",
          base_url: "http://127.0.0.1:8765/v1",
          credential_kind: "none",
          auth_type: "api_key",
          subscription_vendor: null,
          api_key_env: null,
          api_key_file: null,
          api_version: null,
          aws_region_name: null,
          aws_profile_name: null,
          description: "Heartwood-managed synthetic coder",
          credential_status: "configured",
        },
      ],
    };
  };
  const isProjectInitialized = (): boolean => {
    projectInitialized ??= !new URL(page.url()).searchParams.has("new-project");
    return projectInitialized;
  };
  const startupPlan = (): StartupPlan => {
    ensureConfiguredModelFromPage();
    const initialized = isProjectInitialized();
    const ready = modelSettings.active_profile !== null;
    const phase =
      !initialized ? "project-review"
      : ready ? "ready"
      : "connection-required";
    return {
      phase,
      interface: "web",
      platform_id: "generic",
      project_root: "/workspace/synthetic-analysis",
      state_root: "/workspace/synthetic-analysis/.heartwood",
      summary:
        phase === "project-review" ?
          "Review this project before Heartwood creates private project state."
        : phase === "ready" ? "Heartwood is ready in the web interface."
        : "Choose where the model runs.",
      next_action:
        phase === "project-review" ?
          "Confirm the project and choose a model connection."
        : phase === "ready" ? "Start or resume a session."
        : "Select a model connection in setup.",
      access_url: "http://127.0.0.1:4173/",
      requires_compute: false,
      requires_confirmation: false,
      interface_supported: true,
      readiness: {
        state: ready ? "ready" : "setup-required",
        platform_id: "generic",
        project_root: "/workspace/synthetic-analysis",
        state_root: "/workspace/synthetic-analysis/.heartwood",
        evidence: [],
        checks: [
          {
            check_id: "project-state",
            status: initialized ? "pass" : "warning",
            summary:
              initialized ?
                "Project state is ready"
              : "Confirm this project before Heartwood creates private state",
          },
          {
            check_id: "model",
            status: ready ? "pass" : "warning",
            summary: ready ? "Active model: local" : "No active model selected",
          },
        ],
      },
      capabilities,
    };
  };

  await page.route("**/project/capabilities", (route) =>
    json(route, capabilities),
  );
  await page.route("**/project/startup**", (route) =>
    json(route, startupPlan()),
  );
  await page.route("**/project/initialize", async (route) => {
    projectInitialized = true;
    await json(route, startupPlan());
  });

  await page.route("**/project/readiness", (route) =>
    json(route, startupPlan().readiness),
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
    if (sessionId === "default" && request.method() === "POST") {
      const current =
        sessions.find((session) => session.session_id === "session-main") ??
        summary("session-main", "Main session", 0);
      sessions = [
        current,
        ...sessions.filter((session) => session !== current),
      ];
      await json(route, current);
      return;
    }
    if (resource === "events") {
      await json(route, {
        events: sessionId === "session-test" ? sessionEvents : [],
        projection:
          sessionId === "session-test" ? sessionProjection : (
            emptyProjection(sessionId)
          ),
      });
      return;
    }
    if (resource === "workspace") {
      const workspaceResource = parts[sessionsIndex + 3];
      const limits = {
        max_change_entries: 200,
        max_diff_bytes: 262_144,
        max_file_bytes: 262_144,
        max_file_lines: 4_000,
        max_tree_depth: 8,
        max_tree_entries: 1_000,
      };
      if (workspaceResource === "tree") {
        await json(route, {
          schema_version: "heartwood.workspace-tree.v1",
          path: ".",
          status: "available",
          entries: [
            {
              path: "analysis.py",
              name: "analysis.py",
              kind: "file",
              depth: 1,
              size_bytes: 17,
            },
          ],
          truncated: false,
          limits,
        });
        return;
      }
      if (workspaceResource === "file") {
        await json(route, {
          schema_version: "heartwood.workspace-file.v1",
          path: "analysis.py",
          status: "available",
          content: "cohort_size = 42\n",
          size_bytes: 17,
          bytes_read: 17,
          line_count: 1,
          truncated: false,
          message: null,
        });
        return;
      }
      if (workspaceResource === "changes") {
        await json(route, {
          schema_version: "heartwood.workspace-changes.v1",
          status: "available",
          source: "git",
          changes: [
            {
              path: "analysis.py",
              status: "modified",
              source: "git",
              action_ids: [],
            },
          ],
          truncated: false,
          message: null,
          limits,
        });
        return;
      }
      if (workspaceResource === "diff") {
        await json(route, {
          schema_version: "heartwood.workspace-diff.v1",
          path: "analysis.py",
          status: "available",
          source: "git",
          original: "cohort_size = 20\n",
          modified: "cohort_size = 42\n",
          truncated: false,
          message: null,
        });
        return;
      }
    }
    if (resource === "commands") {
      const payload = request.postDataJSON() as {
        command_id?: string;
        kind?: string;
        payload?: { prompt?: string };
      };
      if (payload.kind === "chat") {
        await new Promise((resolve) => setTimeout(resolve, 1_500));
      }
      const resolvesApproval =
        sessionId === "session-test" &&
        (payload.kind === "approve" || payload.kind === "deny");
      const nextEvents =
        resolvesApproval ?
          [
            event(sessionEvents.length, "confirmation.resolved", {
              decision: payload.kind === "approve" ? "approved" : "denied",
              tool_call_id: "session-test-toolcall-0",
            }),
          ]
        : [];
      sessionEvents = [...sessionEvents, ...nextEvents];
      if (resolvesApproval) {
        const approved = payload.kind === "approve";
        sessionProjection = {
          ...sessionProjection,
          eventCount: sessionEvents.length,
          revision:
            sessionEvents.at(-1)?.sequence ?? sessionProjection.revision,
          pendingApproval: null,
          lifecycle: {
            status: "idle",
            canPause: false,
            canResume: false,
            canSteer: true,
          },
          researcherStatus:
            approved ?
              sessionProjection.researcherStatus
            : {
                code: "denied",
                label: "Action Set Rejected",
                detail:
                  "The proposed actions were not run. Heartwood is ready for revised guidance.",
                tone: "attention",
                recoverable: true,
              },
          actions: sessionProjection.actions.map((action) => ({
            ...action,
            state: approved ? ("approved" as const) : ("rejected" as const),
            decision: approved ? ("approved" as const) : ("rejected" as const),
            updatedSequence:
              sessionEvents.at(-1)?.sequence ?? action.updatedSequence,
          })),
          availableCommands: ["chat"],
        };
      } else if (
        sessionId === "session-test" &&
        payload.kind === "chat" &&
        payload.payload?.prompt
      ) {
        sessionProjection = {
          ...sessionProjection,
          eventCount: sessionProjection.eventCount + 1,
          revision: sessionProjection.revision + 1,
          conversation: [
            ...sessionProjection.conversation,
            {
              id: `local-${payload.command_id ?? "chat"}`,
              sequence: sessionProjection.revision + 1,
              role: "user",
              label: "You",
              content: payload.payload.prompt,
              detail: null,
              technicalDetail: null,
            },
          ],
        };
      }
      await json(route, { events: nextEvents, projection: sessionProjection });
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
      schema_version: "heartwood.local-model-catalog.v2",
      snapshot_schema_version: "heartwood.model-snapshot-catalog.v3",
      artifacts: [],
      snapshots: [],
      models: [],
      downloads: [],
      transfers: [],
      gpu_environment: {
        platform_id: "generic",
        capacities: [],
      },
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
        license_id: "Apache-2.0",
        license_posture: "Source model card reports apache-2.0.",
        catalog_source: "user-selected",
        context_window: 32_768,
        maximum_context_window: 32_768,
        precision: "GGUF Q4_K_M",
        tier: "standard",
        qualification: "qualified",
        minimum_gpu_count: 0,
        minimum_gpu_memory_bytes: 0,
        recommended_ram_bytes: 16 * 1024 * 1024 * 1024,
        recommended_disk_bytes: 8 * 1024 * 1024 * 1024,
        tool_call_parser: null,
        tensor_parallel_size: 1,
        startup_seconds_min: 5,
        startup_seconds_max: 30,
        download_policy: null,
        allow_patterns: [],
        ignore_patterns: [],
        validated_platforms: ["ci"],
        qualification_test: "synthetic-browser-e2e-v1",
        artifact_sha256: "a".repeat(64),
        minimum_resource_envelope:
          "Estimated minimum: 4 CPU cores and 12 GB RAM.",
        recommended_resource_envelope:
          "Recommended: 8 CPU cores and 16 GB RAM.",
        active: false,
        available: true,
        selected: false,
        availability_reason: "Available on this deployment",
      },
      selection_reason:
        "Selected a balanced single-file GGUF variant for the CPU runtime.",
    }),
  );
  await page.route("**/settings/models/catalog", (route) => {
    const payload = route.request().postDataJSON() as {
      connection_id: string;
    };
    const connection = modelSettings.connections.find(
      (candidate) => candidate.connection_id === payload.connection_id,
    );
    return json(route, {
      schema_version: "heartwood.model-catalog.v1",
      connection,
      models: [
        {
          model_id: "research-coder",
          display_name: "Research Coder",
          execution_model: "openai/research-coder",
          availability: "available",
          reason: "Verified by the pinned OpenHands SDK",
          context_window: 32_768,
          supports_tools: true,
        },
      ],
      refreshed_at: 1_783_683_200,
    });
  });
  await page.route("**/settings/models/connect", async (route) => {
    const payload = route.request().postDataJSON() as {
      connection_id: string;
      model_id: string;
    };
    const connection = modelSettings.connections.find(
      (candidate) => candidate.connection_id === payload.connection_id,
    );
    if (!connection)
      throw new Error("synthetic model connection is unavailable");
    const profile = {
      profile_id: payload.connection_id,
      model: `openai/${payload.model_id}`,
      policy_endpoint: connection.policy_endpoint,
      capability_tier: "supervised",
      base_url: connection.base_url,
      credential_kind: connection.credential_kind,
      auth_type: connection.auth_type,
      subscription_vendor: connection.subscription_vendor,
      api_key_env: connection.api_key_env,
      api_key_file: null,
      api_version: null,
      aws_region_name: null,
      aws_profile_name: null,
      description: `${connection.label}: Research Coder`,
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
      credential_isolation: credentialIsolation(),
      action_confirmation_mode: "always-confirm",
      policy_decision: {
        decision: "allow",
        endpoint: "https://api.openai.com/v1/chat/completions",
        reason: "Configured route allowed",
      },
    }),
  );
  await page.route("**/settings/actions", (route) =>
    json(route, {
      schema_version: "heartwood.action-settings.v1",
      confirmation_mode: "always-confirm",
      scope_description:
        "Shared by every Heartwood interface in this project and applied to future action sets.",
      presentation: {
        risk_labels: {
          high: "High Risk",
          low: "Low Risk",
          medium: "Medium Risk",
          unknown: "Not Classified",
        },
        state_labels: {
          approved: "Approved",
          "awaiting-review": "Awaiting Review",
          failed: "Failed",
          "outcome-unknown": "Outcome Unknown",
          proposed: "Proposed",
          rejected: "Rejected",
          running: "Running",
          succeeded: "Succeeded",
        },
        tool_labels: {
          file_editor: "File Change",
          terminal: "Terminal Command",
        },
        other_tool_label_template: "{tool_name} Action",
        unknown_risk_label: "Not Classified",
        unknown_tool_label: "Tool Action",
      },
      change_allowed: true,
      change_blocked_reason: null,
      modes: [
        {
          mode: "always-confirm",
          command_value: "ask-every-time",
          label: "Review Every Action",
          description:
            "Heartwood pauses before every proposed action set so you can inspect it before anything runs.",
          automatic_risks: [],
          reviewed_risks: ["low", "medium", "high", "unknown"],
          recommended: true,
          allowed: true,
          unavailable_reason: null,
        },
        {
          mode: "confirm-risky",
          command_value: "auto-approve-low-risk",
          label: "Low-Risk Automation",
          description:
            "An action set continues automatically only when every action is low risk. Any medium-, high-, or unclassified-risk action pauses the complete set for review.",
          automatic_risks: ["low"],
          reviewed_risks: ["medium", "high", "unknown"],
          recommended: false,
          allowed: true,
          unavailable_reason: null,
        },
      ],
    }),
  );
  await page.route("**/settings/models", (route) => {
    ensureConfiguredModelFromPage();
    return json(route, modelSettings);
  });
  await page.route("**/settings/skills", (route) =>
    json(route, {
      skills: [
        {
          name: "omop-cohort-summary",
          skill_id: "heartwood.research.omop-cohort-summary",
          version: "1.0.0",
          description: "Target-condition cohort and aggregate quality checks",
          source: "bundled",
          source_id: "heartwood",
          review: "repository-reviewed",
          status: "active",
          approval_summary: "Reads localized synthetic OMOP tables.",
          declared_tools: ["read-synthetic-tables", "write-aggregate-json"],
          requires_network: false,
          phi_risk: "reads-phi",
          data_access_summary: "Reads potentially identifiable row-level data",
          dataset_types: ["omop-cdm"],
          controlled_data_ready: false,
          tree_sha256: "a".repeat(64),
          source_revision: null,
          archive_size: null,
          revocation_reason: null,
          compatibility_reason: null,
          installable: false,
        },
        {
          name: "baseline-model",
          skill_id: "heartwood.research.baseline-model",
          version: "1.0.0",
          description: "Training-only age baseline with aggregate diagnostics",
          source: "bundled",
          source_id: "heartwood",
          review: "repository-reviewed",
          status: "active",
          approval_summary:
            "Reads synthetic tables and writes model diagnostics.",
          declared_tools: ["read-synthetic-tables", "write-aggregate-json"],
          requires_network: false,
          phi_risk: "reads-phi",
          data_access_summary: "Reads potentially identifiable row-level data",
          dataset_types: ["omop-cdm"],
          controlled_data_ready: false,
          tree_sha256: "b".repeat(64),
          source_revision: null,
          archive_size: null,
          revocation_reason: null,
          compatibility_reason: null,
          installable: false,
        },
        {
          name: "aggregate-export",
          skill_id: "heartwood.research.aggregate-export",
          version: "1.0.0",
          description: "Aggregate export Skill",
          source: "bundled",
          source_id: "heartwood",
          review: "repository-reviewed",
          status: "active",
          approval_summary: "Writes reviewed aggregate output.",
          declared_tools: ["write-aggregate-json"],
          requires_network: false,
          phi_risk: "none",
          data_access_summary: "No row-level PHI access declared",
          dataset_types: ["omop-cdm"],
          controlled_data_ready: false,
          tree_sha256: "c".repeat(64),
          source_revision: null,
          archive_size: null,
          revocation_reason: null,
          compatibility_reason: null,
          installable: false,
        },
      ],
    }),
  );
  await page.route("**/settings/specialists", (route) =>
    json(route, {
      specialists: [
        {
          specialist_id: "research-planner",
          label: "Research Planner",
          description: "Develops a concise, sequential analysis plan.",
          presentation_summary:
            "Advisory · Uses the active model · Up to 12 steps",
          capability: "advisory",
          availability: "available",
          unavailable_reason: null,
          model_route: "inherit",
          tools: [],
          skills: [],
          permission_mode: "always_confirm",
          max_iterations: 12,
          max_budget_usd: 1,
        },
        {
          specialist_id: "analysis-implementer",
          label: "Analysis Implementer",
          description: "Implements bounded research-analysis changes.",
          presentation_summary:
            "Project actions · Uses the active model · Up to 40 steps",
          capability: "project-actions",
          availability: "unavailable",
          unavailable_reason:
            "Tool-enabled specialists require restart-safe child review.",
          model_route: "inherit",
          tools: ["terminal", "heartwood_project_file_editor"],
          skills: ["omop-cohort-summary"],
          permission_mode: "always_confirm",
          max_iterations: 40,
          max_budget_usd: 2,
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
