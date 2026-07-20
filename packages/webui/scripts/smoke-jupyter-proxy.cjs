/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

/* global clearTimeout */

const { Buffer } = require("node:buffer");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const scriptDir = __dirname;
const packageRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(packageRoot, "../..");
const heartwoodExecutable = path.join(repoRoot, ".venv", "bin", "heartwood");
const webRoot = path.join(packageRoot, "dist");
const workspace = fs.mkdtempSync(
  path.join(os.tmpdir(), "heartwood-jupyter-proxy-"),
);
const gatewayPort = process.env.HEARTWOOD_WEB_JUPYTER_GATEWAY_PORT || "8776";
const proxyPort = process.env.HEARTWOOD_WEB_JUPYTER_PROXY_PORT || "8777";
const servicePrefix =
  process.env.HEARTWOOD_WEB_JUPYTER_SERVICE_PREFIX ||
  "/proxy/heartwood-ci/saturn-smoke/jupyter/";
const externalBasePath = `${normalizePrefix(servicePrefix)}proxy/${gatewayPort}/`;
const externalOrigin = `http://127.0.0.1:${proxyPort}`;
const externalBaseUrl = `${externalOrigin}${externalBasePath}`;
const gatewayOrigin = `http://127.0.0.1:${gatewayPort}`;
const logs = [];
const proxySockets = new Set();
const verbose = process.env.HEARTWOOD_WEB_SMOKE_VERBOSE === "1";

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

async function main() {
  if (!fs.existsSync(path.join(webRoot, "index.html"))) {
    throw new Error("web UI assets are missing; run npm run build first");
  }

  const gateway = startGateway();
  let proxy;

  try {
    trace("waiting for gateway");
    await Promise.race([waitForServer(gatewayOrigin), gateway.spawnError]);
    trace("starting proxy");
    proxy = await startProxy();
    trace("waiting for external proxy route");
    await waitForServer(externalBaseUrl);
    trace("verifying web assets");
    await verifyWebAssets(externalBaseUrl);
    trace("verifying session routes");
    await verifySessionRoutes(externalBaseUrl);
  } finally {
    trace("cleaning up");
    if (proxy !== undefined) {
      await closeServer(proxy);
      trace("closed proxy");
    }
    terminateProcessGroup(gateway.process);
    trace("terminated gateway process group");
    await waitForExit(gateway.process);
    trace("gateway process exited");
    fs.rmSync(workspace, { force: true, recursive: true });
  }
}

function startGateway() {
  const server = spawn(
    heartwoodExecutable,
    [
      "gateway",
      "serve",
      "--host",
      "127.0.0.1",
      "--port",
      gatewayPort,
      "--web-root",
      webRoot,
      "--base-path",
      "/",
    ],
    {
      cwd: workspace,
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

  return { process: server, spawnError };
}

async function startProxy() {
  const server = http.createServer((request, response) => {
    const requestUrl = request.url ?? "/";
    const incoming = new URL(requestUrl, externalOrigin);
    if (!incoming.pathname.startsWith(externalBasePath)) {
      response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      response.end("not found");
      return;
    }

    const targetPath =
      incoming.pathname.slice(externalBasePath.length - 1) || "/";
    const upstream = http.request(
      {
        headers: Object.assign({}, request.headers, {
          host: `127.0.0.1:${gatewayPort}`,
        }),
        hostname: "127.0.0.1",
        method: request.method,
        path: `${targetPath}${incoming.search}`,
        port: Number(gatewayPort),
      },
      (upstreamResponse) => {
        response.writeHead(
          upstreamResponse.statusCode ?? 502,
          upstreamResponse.headers,
        );
        upstreamResponse.pipe(response);
      },
    );
    upstream.on("error", (error) => {
      response.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
      response.end(String(error));
    });
    request.pipe(upstream);
  });
  server.on("connection", (socket) => {
    proxySockets.add(socket);
    socket.once("close", () => {
      proxySockets.delete(socket);
    });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(Number(proxyPort), "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });
  return server;
}

async function verifyWebAssets(baseUrl) {
  const html = await fetchText(baseUrl);
  if (!html.includes('<div id="root"></div>')) {
    throw new Error(
      "Jupyter proxy web UI index did not contain the React mount point",
    );
  }
  const assetMatch = /(?:src|href)="(\.\/assets\/[^"]+)"/.exec(html);
  const assetPath = assetMatch === null ? undefined : assetMatch[1];
  if (assetPath === undefined) {
    throw new Error(
      "Jupyter proxy web UI index did not reference a built asset",
    );
  }
  const asset = await fetchText(new URL(assetPath, baseUrl).toString());
  if (asset.length === 0) {
    throw new Error("Jupyter proxy web UI asset was empty");
  }
}

async function verifySessionRoutes(baseUrl) {
  const createdSession = await fetchJson(
    new URL("sessions", baseUrl).toString(),
    withConnectionClose({
      body: JSON.stringify({ title: "Jupyter proxy smoke" }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    }),
  );
  if (typeof createdSession.session_id !== "string") {
    throw new Error("Jupyter proxy session route did not create a session");
  }
  const sessionId = createdSession.session_id;
  const commandResponse = await fetchJson(
    new URL(`sessions/${sessionId}/commands`, baseUrl).toString(),
    {
      body: JSON.stringify({
        actor_id: "synthetic-user",
        command_id: "jupyter-proxy-smoke-pause",
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
  if (!commandKinds.includes("session.paused")) {
    throw new Error("Jupyter proxy command route did not return session state");
  }

  const replayResponse = await fetchJson(
    new URL(`sessions/${sessionId}/events?after=0`, baseUrl).toString(),
  );
  const replayEvents =
    Array.isArray(replayResponse.events) ? replayResponse.events : [];
  if (!replayEvents.some((event) => event.sequence === 1)) {
    throw new Error(
      "Jupyter proxy replay route did not return persisted events",
    );
  }

  await fetchJson(
    new URL(`sessions/${sessionId}/commands`, baseUrl).toString(),
    {
      body: JSON.stringify({
        actor_id: "synthetic-user",
        command_id: "jupyter-proxy-smoke-audit-export",
        created_at: "2026-01-01T00:00:01Z",
        kind: "audit.export",
        payload: {},
        schema_version: "heartwood.session-command.v1",
        session_id: sessionId,
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    },
  );
  const auditExport = await fetchJson(
    new URL(`sessions/${sessionId}/audit-export`, baseUrl).toString(),
  );
  if (
    auditExport.filename !== `${sessionId}-audit.jsonl` ||
    !auditExport.content.includes("audit.export.recorded")
  ) {
    throw new Error("Jupyter proxy did not deliver the scrubbed audit export");
  }

  const stream = await fetchSseEvent(
    new URL(`sessions/${sessionId}/events/stream?after=0`, baseUrl).toString(),
  );
  if (
    !stream.includes("event: heartwood-session-events") ||
    !stream.includes("session.paused")
  ) {
    throw new Error("Jupyter proxy SSE route did not stream persisted events");
  }
}

async function waitForServer(url) {
  const deadline = Date.now() + 15000;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, withConnectionClose());
      if (response.ok) {
        await response.arrayBuffer();
        return;
      }
      await response.arrayBuffer();
      lastError = new Error(`server returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await delay(250);
  }
  throw new Error(
    `Jupyter proxy smoke server did not become ready: ${lastError}\n${logs.join("")}`,
  );
}

async function fetchText(url) {
  const response = await fetch(url, withConnectionClose());
  if (!response.ok) {
    throw new Error(`GET ${url} returned ${response.status}`);
  }
  return response.text();
}

async function fetchJson(url, init) {
  const response = await fetch(url, withConnectionClose(init));
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      `gateway request returned ${response.status}: ${JSON.stringify(payload)}`,
    );
  }
  return payload;
}

async function fetchSseEvent(url) {
  const response = await fetch(url, withConnectionClose());
  if (!response.ok) {
    throw new Error(`SSE ${url} returned ${response.status}`);
  }
  if (response.body === null) {
    throw new Error("SSE response did not expose a readable body");
  }
  const reader = response.body.getReader();
  let buffer = "";
  try {
    while (!hasSseFrame(buffer)) {
      const chunk = await readWithTimeout(reader);
      if (chunk.done) {
        break;
      }
      buffer += Buffer.from(chunk.value).toString("utf8");
    }
  } finally {
    await reader.cancel();
  }
  return buffer;
}

async function readWithTimeout(reader) {
  let timer;
  try {
    return await Promise.race([
      reader.read(),
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          reject(new Error("timed out waiting for SSE event"));
        }, 5000);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function closeServer(server) {
  for (const socket of proxySockets) {
    socket.destroy();
  }
  let timer;
  try {
    await Promise.race([
      new Promise((resolve, reject) => {
        server.close((error) => {
          if (error === undefined) {
            resolve();
          } else {
            reject(error);
          }
        });
      }),
      new Promise((resolve) => {
        timer = setTimeout(resolve, 5000);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
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

async function delay(milliseconds) {
  await new Promise((resolve) => {
    setTimeout(resolve, milliseconds);
  });
}

function normalizePrefix(prefix) {
  const leading = prefix.startsWith("/") ? prefix : `/${prefix}`;
  return leading.endsWith("/") ? leading : `${leading}/`;
}

function hasSseFrame(buffer) {
  return buffer.includes("\n\n") || buffer.includes("\r\n\r\n");
}

function withConnectionClose(init = {}) {
  return Object.assign({}, init, {
    headers: Object.assign({ Connection: "close" }, init.headers ?? {}),
  });
}

function trace(message) {
  if (verbose) {
    console.log(message);
  }
}
