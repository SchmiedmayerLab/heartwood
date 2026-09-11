/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

/* global clearTimeout, document, window */

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { chromium, expect } = require("@playwright/test");
const AccessibilityScanner = require("@axe-core/playwright").default;

const scriptDir = __dirname;
const packageRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(packageRoot, "../..");
const heartwoodExecutable = path.join(repoRoot, ".venv", "bin", "heartwood");
const webRoot = path.join(packageRoot, "dist");
const workspace = fs.mkdtempSync(
  path.join(os.tmpdir(), "heartwood-web-gateway-"),
);
const logs = [];

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

async function main() {
  if (!fs.existsSync(path.join(webRoot, "index.html"))) {
    throw new Error("web UI assets are missing; run npm run build first");
  }

  const port =
    process.env.HEARTWOOD_WEB_SMOKE_PORT ||
    String(await availableLoopbackPort());
  const basePath = `/proxy/${port}/`;
  const origin = `http://127.0.0.1:${port}`;
  const proxiedBaseUrl = `${origin}${basePath}`;
  const server = spawn(
    heartwoodExecutable,
    [
      "gateway",
      "serve",
      "--host",
      "127.0.0.1",
      "--port",
      port,
      "--web-root",
      webRoot,
      "--base-path",
      basePath,
    ],
    {
      cwd: workspace,
      detached: true,
      env: Object.assign({}, process.env, {
        HOME: path.join(workspace, ".test-home"),
        OH_PERSISTENCE_DIR: path.join(workspace, ".openhands"),
        XDG_CONFIG_HOME: path.join(workspace, ".config"),
        XDG_CACHE_HOME: path.join(workspace, ".cache"),
        UV_CACHE_DIR: path.join(repoRoot, ".uv-cache"),
      }),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  server.stdout.on("data", (chunk) => logs.push(String(chunk)));
  server.stderr.on("data", (chunk) => logs.push(String(chunk)));

  const spawnError = new Promise((_, reject) => {
    server.on("error", (error) => {
      if (error.code === "ENOENT") {
        reject(new Error("heartwood executable missing; run uv sync --locked"));
      } else {
        reject(error);
      }
    });
  });

  try {
    await Promise.race([waitForServer(proxiedBaseUrl, server), spawnError]);
    const html = await fetchText(proxiedBaseUrl);
    if (!html.includes('<div id="root"></div>')) {
      throw new Error(
        "proxied web UI index did not contain the React mount point",
      );
    }
    const assetMatch = /(?:src|href)="(\.\/assets\/[^"]+)"/.exec(html);
    const assetPath = assetMatch === null ? undefined : assetMatch[1];
    if (assetPath === undefined) {
      throw new Error("proxied web UI index did not reference a built asset");
    }
    const asset = await fetchText(
      new URL(assetPath, proxiedBaseUrl).toString(),
    );
    if (asset.length === 0) {
      throw new Error("proxied web UI asset was empty");
    }

    await inspectResearchSetup(proxiedBaseUrl);

    const createdSession = await fetchJson(`${origin}${basePath}sessions`, {
      body: JSON.stringify({ title: "Web smoke session" }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    });
    if (typeof createdSession.session_id !== "string") {
      throw new Error("proxied gateway did not create a session");
    }
    const sessionId = createdSession.session_id;
    const renamedSession = await fetchJson(
      `${origin}${basePath}sessions/${sessionId}`,
      {
        body: JSON.stringify({ title: "Renamed web smoke session" }),
        headers: { "Content-Type": "application/json" },
        method: "PATCH",
      },
    );
    if (renamedSession.title !== "Renamed web smoke session") {
      throw new Error("proxied gateway did not rename the session");
    }
    const sessionList = await fetchJson(`${origin}${basePath}sessions`);
    if (
      !Array.isArray(sessionList.sessions) ||
      !sessionList.sessions.some((session) => session.session_id === sessionId)
    ) {
      throw new Error("proxied gateway did not list the created session");
    }

    const commandResponse = await fetchJson(
      `${origin}${basePath}sessions/${sessionId}/commands`,
      {
        body: JSON.stringify({
          actor_id: "synthetic-user",
          command_id: "web-smoke-pause",
          created_at: "2026-01-01T00:00:00Z",
          kind: "pause",
          payload: {},
          schema_version: "heartwood.session-command.v1",
          session_id: sessionId,
        }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      },
    );
    const commandEvents =
      Array.isArray(commandResponse.events) ? commandResponse.events : [];
    const commandKinds = commandEvents.map((event) => event.kind);
    if (
      !commandKinds.includes("command.received") ||
      !commandKinds.includes("error.recorded")
    ) {
      throw new Error(
        "proxied gateway did not reject an unavailable idle-session command",
      );
    }
    const projection = commandResponse.projection;
    if (
      projection?.lifecycle?.status !== "idle" ||
      projection?.revision !== commandEvents.at(-1)?.sequence ||
      projection?.availableCommands?.join(",") !== "chat"
    ) {
      throw new Error(
        "proxied gateway command route did not return its authoritative projection",
      );
    }

    const replayResponse = await fetchJson(
      `${origin}${basePath}sessions/${sessionId}/events?after=0`,
    );
    const replayEvents =
      Array.isArray(replayResponse.events) ? replayResponse.events : [];
    const replaySequences = replayEvents.map((event) => event.sequence);
    if (!replaySequences.includes(1)) {
      throw new Error(
        "proxied gateway replay route did not return persisted events",
      );
    }

    await fetchJson(`${origin}${basePath}sessions/${sessionId}/commands`, {
      body: JSON.stringify({
        actor_id: "synthetic-user",
        command_id: "web-smoke-audit-export",
        created_at: "2026-01-01T00:00:01Z",
        kind: "audit.export",
        payload: {},
        schema_version: "heartwood.session-command.v1",
        session_id: sessionId,
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    });
    const auditExport = await fetchJson(
      `${origin}${basePath}sessions/${sessionId}/audit-export`,
    );
    if (
      auditExport.filename !== `${sessionId}-audit.jsonl` ||
      !auditExport.content.includes("audit.export.recorded")
    ) {
      throw new Error(
        "proxied gateway did not deliver the scrubbed audit export",
      );
    }
  } finally {
    terminateProcessGroup(server);
    await waitForExit(server);
    fs.rmSync(workspace, { force: true, recursive: true });
  }
}

async function inspectResearchSetup(url) {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 1000 },
    });
    const page = await context.newPage();
    page.on("response", (response) => {
      if (response.status() >= 500) {
        console.error(
          `Gateway failure: ${response.status()} ${response.url()}`,
        );
      }
    });
    await page.goto(url);
    await expect(
      page.getByRole("heading", { name: "Set up Heartwood" }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Use this project", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Use this project", exact: true }),
    ).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(
      page.getByRole("heading", { name: "Main session", exact: true }),
    ).toBeVisible();
    await page.getByRole("tab", { name: "Research", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Research Workflows" }),
    ).toBeVisible();
    await expect(page.getByLabel("Dataset", { exact: true })).toBeVisible();
    await page.getByLabel("Dataset", { exact: true }).fill("synthetic.csv");
    await page.getByRole("tab", { name: "Conversation", exact: true }).click();
    await page.getByRole("tab", { name: "Research", exact: true }).click();
    await expect(page.getByLabel("Dataset", { exact: true })).toHaveValue(
      "synthetic.csv",
    );
    await expect(
      page.getByRole("button", { name: "Start Workflow" }),
    ).toBeDisabled();
    const catalog = await fetchJson(`${url}research/workflows`);
    if (
      !catalog.workflows.some(
        (entry) =>
          entry.definition.workflow_id === "baseline-analysis" &&
          entry.available,
      )
    ) {
      throw new Error("baseline workflow is missing from the shared catalog");
    }
    await page.getByText("Project Experiment Records", { exact: true }).click();
    const records = page.locator(".project-experiments");
    await expect(
      records.getByText("No experiments recorded in this project."),
    ).toBeVisible();
    const exported = await fetchJson(`${url}research/experiments/export`);
    const downloadPromise = page.waitForEvent("download");
    await records.getByRole("button", { name: "Export Records" }).click();
    const downloaded = await downloadPromise;
    expect(fs.readFileSync(await downloaded.path(), "utf8")).toBe(
      exported.jsonl,
    );
    await expect(records.getByRole("status")).toContainText(exported.sha256);
    for (const theme of ["light", "dark"]) {
      if (theme === "dark")
        await page.getByRole("button", { name: "Switch to dark mode" }).click();
      for (const width of [1440, 390]) {
        await page.setViewportSize({ width, height: 1000 });
        await page.evaluate(() =>
          Promise.allSettled(
            document
              .getAnimations()
              .filter(
                (animation) =>
                  animation.effect?.getTiming().iterations !== Infinity,
              )
              .map((animation) => animation.finished),
          ),
        );
        const violations = (await new AccessibilityScanner({ page }).analyze())
          .violations;
        if (violations.length) throw new Error(JSON.stringify(violations));
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
          ),
        ).toBe(true);
        const screenshots = process.env.HEARTWOOD_WEB_SMOKE_SCREENSHOT_DIR;
        if (screenshots) {
          fs.mkdirSync(screenshots, { recursive: true });
          await page.screenshot({
            path: path.join(screenshots, `research-${theme}-${width}.png`),
            fullPage: true,
          });
        }
      }
    }
  } catch (error) {
    console.error(logs.join(""));
    throw error;
  } finally {
    await browser.close();
  }
}

async function waitForServer(url, server) {
  const deadline = Date.now() + 15000;
  let lastError;
  while (Date.now() < deadline) {
    if (server.exitCode !== null || server.signalCode !== null) {
      throw new Error(
        `gateway process exited before becoming ready\n${logs.join("")}`,
      );
    }
    try {
      const response = await fetch(url);
      if (response.ok) {
        return;
      }
      lastError = new Error(`server returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => {
      setTimeout(resolve, 250);
    });
  }
  throw new Error(
    `gateway server did not become ready: ${lastError}\n${logs.join("")}`,
  );
}

async function availableLoopbackPort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    server.close();
    throw new Error("unable to allocate a loopback port");
  }
  await new Promise((resolve, reject) => {
    server.close((error) => (error === undefined ? resolve() : reject(error)));
  });
  return address.port;
}

async function fetchText(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`GET ${url} returned ${response.status}`);
  }
  return response.text();
}

async function fetchJson(url, init) {
  const response = await fetch(url, init);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      `gateway request returned ${response.status}: ${JSON.stringify(payload)}`,
    );
  }
  return payload;
}

async function waitForExit(child) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return;
  }
  await new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(killTimer);
      clearTimeout(resolveTimer);
      child.off("close", finish);
      child.off("error", finish);
      child.off("exit", finish);
      resolve();
    };
    const resolveTimer = setTimeout(finish, 6000);
    const killTimer = setTimeout(() => {
      child.kill("SIGKILL");
    }, 5000);
    child.once("close", finish);
    child.once("error", finish);
    child.once("exit", finish);
  });
}

function terminateProcessGroup(child) {
  if (child.pid === undefined) {
    child.kill("SIGTERM");
    return;
  }
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch {
    child.kill("SIGTERM");
  }
}
