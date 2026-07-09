/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

/* global clearTimeout */

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const scriptDir = __dirname;
const packageRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(packageRoot, "../..");
const heartwoodExecutable = path.join(repoRoot, ".venv", "bin", "heartwood");
const webRoot = path.join(packageRoot, "dist");
const workspace = fs.mkdtempSync(
  path.join(os.tmpdir(), "heartwood-web-gateway-"),
);
const port = process.env.HEARTWOOD_WEB_SMOKE_PORT || "8767";
const basePath = `/proxy/${port}/`;
const origin = `http://127.0.0.1:${port}`;
const proxiedBaseUrl = `${origin}${basePath}`;
const logs = [];

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

async function main() {
  if (!fs.existsSync(path.join(webRoot, "index.html"))) {
    throw new Error("web UI assets are missing; run npm run build first");
  }

  const server = spawn(
    heartwoodExecutable,
    [
      "--workspace",
      path.join(workspace, "sessions"),
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
      cwd: repoRoot,
      detached: true,
      env: Object.assign({}, process.env, {
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
    await Promise.race([waitForServer(proxiedBaseUrl), spawnError]);
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

    const commandResponse = await fetchJson(
      `${origin}${basePath}sessions/web-smoke/commands`,
      {
        body: JSON.stringify({
          actor_id: "synthetic-user",
          command_id: "web-smoke-detect",
          created_at: "2026-01-01T00:00:00Z",
          kind: "detect",
          payload: {},
          schema_version: "heartwood.session-command.v1",
          session_id: "web-smoke",
        }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      },
    );
    const commandEvents =
      Array.isArray(commandResponse.events) ? commandResponse.events : [];
    const commandKinds = commandEvents.map((event) => event.kind);
    if (!commandKinds.includes("detection.proposed")) {
      throw new Error(
        "proxied gateway command route did not return detection events",
      );
    }

    const replayResponse = await fetchJson(
      `${origin}${basePath}sessions/web-smoke/events?after=0`,
    );
    const replayEvents =
      Array.isArray(replayResponse.events) ? replayResponse.events : [];
    const replaySequences = replayEvents.map((event) => event.sequence);
    if (!replaySequences.includes(1)) {
      throw new Error(
        "proxied gateway replay route did not return persisted events",
      );
    }
  } finally {
    terminateProcessGroup(server);
    await waitForExit(server);
    fs.rmSync(workspace, { force: true, recursive: true });
  }
}

async function waitForServer(url) {
  const deadline = Date.now() + 15000;
  let lastError;
  while (Date.now() < deadline) {
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
