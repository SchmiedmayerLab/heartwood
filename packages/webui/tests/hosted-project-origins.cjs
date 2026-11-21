// This source file is part of the Heartwood open-source project
// SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
// SPDX-License-Identifier: MIT

// Standalone to avoid Playwright Test traces, screenshots, reports and retry artifacts.
const { execFileSync } = require("node:child_process");
const { randomUUID } = require("node:crypto");
const http = require("node:http");
const https = require("node:https");
const assert = require("node:assert/strict");

const hub = "https://proxy-public.heartwood-platform.svc.cluster.local";
const idp = "https://keycloak.heartwood-platform.svc.cluster.local:8443";
const stages = [
  "Hub and Keycloak login",
  "four researcher/project origins",
  "project data separation and cross-origin denial",
  "cross-researcher and nonmember login refusal",
  "project logout and re-entry",
  "Hub logout and independent Hub session",
];
let stage = "browser startup";

const disposableLinux =
  process.platform === "linux" && process.env.HEARTWOOD_NSS_BROWSER === "1";
let fixturePasswords;

function password(account) {
  if (disposableLinux) {
    if (!fixturePasswords) {
      const lines = require("node:fs")
        .readFileSync(0, "utf8")
        .trimEnd()
        .split("\n");
      assert(
        lines.length === 2 &&
          lines.every((value) => /^[a-zA-Z0-9+/]+=*$/.test(value)),
      );
      fixturePasswords = new Map(
        ["researcher-one", "researcher-two"].map((name, index) => [
          name,
          Buffer.from(lines[index], "base64").toString("utf8"),
        ]),
      );
    }
    assert(fixturePasswords.has(account));
    return fixturePasswords.get(account);
  }
  const encoded = execFileSync(
    "kubectl",
    [
      "--context=kind-heartwood-origins-revision",
      "-n",
      "heartwood-platform",
      "get",
      "secret/keycloak-researchers",
      "-o",
      "jsonpath={.data." + account + "}",
    ],
    { stdio: ["ignore", "pipe", "ignore"] },
  );
  assert(encoded.length > 0);
  return Buffer.from(encoded.toString(), "base64").toString();
}

async function signIn(context, account) {
  const page = await context.newPage();
  await page.goto(hub + "/hub/oauth_login");
  const form = page.locator("#kc-form-login");
  await form.waitFor();
  assert.equal(new URL(page.url()).origin, idp);
  assert.equal(new URL(await form.getAttribute("action"), idp).origin, idp);
  await form.locator('[name="username"]').fill(account);
  await form.locator('[name="password"]').fill(password(account));
  await form.locator('[type="submit"]').click();
  await page.waitForURL(
    (url) => url.origin === hub && !url.pathname.includes("oauth_callback"),
  );
  await page.goto(hub + "/hub/home");
  await page.locator(".new-server-name").waitFor();
  return page;
}

async function ready(page) {
  const rename = page.getByRole("button", { name: "Rename session" });
  const use = page.getByRole("button", { name: "Use this project" });
  await rename.or(use).first().waitFor({ timeout: 420000 });
  if (await use.isVisible()) await use.click();
  await rename.waitFor();
  if (
    await page.getByRole("button", { name: "Close", exact: true }).isVisible()
  ) {
    await page.keyboard.press("Escape");
  }
}

async function openProject(context, project) {
  const page = await context.newPage();
  let authorization = false;
  let callback = false;
  let authorizedOrigin;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.origin === idp &&
      url.pathname.endsWith("/protocol/openid-connect/auth") &&
      url.searchParams.get("client_id") === "hw-proj-" + project
    ) {
      const target = URL.parse(url.searchParams.get("redirect_uri"));
      if (!target) return;
      authorizedOrigin = target.origin;
      authorization = target.pathname.endsWith(
        "/" + project + "/oauth_callback",
      );
    }
    if (
      authorization &&
      url.origin === authorizedOrigin &&
      url.pathname.endsWith("/" + project + "/oauth_callback")
    )
      callback = true;
  });
  await page.goto(hub + "/hub/home");
  await page.locator(".new-server-name").waitFor();
  const row = page.locator('tr[data-server-name="' + project + '"]');
  if (await row.locator(".server-link").isVisible()) {
    await row.locator(".server-link").click();
  } else {
    if (await row.count()) {
      await row.locator(".start-server").click();
    } else {
      await page.locator(".new-server-name").fill(project);
      await page.locator(".new-server-btn").click();
    }
    const profile = page.locator(
      'input[name="profile"][value="' + project + '"]',
    );
    await profile.waitFor({ state: "attached" });
    await page
      .locator('form:has(input[name="profile"]) [type="submit"]')
      .click();
  }

  await ready(page);
  const location = new URL(page.url());
  const match = /^\/user\/(r[0-9a-f]{32})\/([a-z0-9-]+)\/$/.exec(
    location.pathname,
  );
  assert(match && match[2] === project);
  assert.equal(
    location.origin,
    "https://" + match[1] + "." + project + ".projects.heartwood.test",
  );
  assert.equal(location.hash, "");
  assert(authorization && callback && authorizedOrigin === location.origin);
  // Retain only the canonical entry, without query, launch fragment, or credentials.
  return {
    page,
    origin: location.origin,
    entry: location.origin + location.pathname,
    user: match[1],
  };
}

async function newAnalysis(server) {
  const created = server.page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.href === server.entry + "sessions" &&
      response.request().method() === "POST"
    );
  });
  const [response] = await Promise.all([
    created,
    server.page.getByRole("button", { name: "New analysis" }).click(),
  ]);
  assert.equal(response.status(), 201);
  await server.page
    .getByRole("heading", { name: "Untitled session", exact: true })
    .waitFor();
}

async function cannotRead(from, target) {
  const url = target.entry + "sessions";
  const network = await from.page.context().newCDPSession(from.page);
  let requestId;
  let corsDenied = false;
  network.on("Network.requestWillBeSent", (event) => {
    if (event.request.url === url && event.request.method === "GET")
      requestId = event.requestId;
  });
  network.on("Network.loadingFailed", (event) => {
    if (event.requestId === requestId && event.corsErrorStatus)
      corsDenied = true;
  });
  try {
    await network.send("Network.enable");
    const status = await from.page.evaluate(async (endpoint) => {
      try {
        return (
          await fetch(endpoint, { credentials: "include", redirect: "error" })
        ).status;
      } catch {
        return null;
      }
    }, url);
    // Synchronize the CDP connection before reading the terminal network event.
    await network.send("Runtime.evaluate", { expression: "0" });
    assert(status === 401 || status === 403 || (status === null && corsDenied));
  } finally {
    await network.detach();
  }
}

async function requireOwnLaunchRefusal(home, user) {
  assert.equal(new URL(home.url()).origin, hub);
  const refused = await home.evaluate(async (researcher) => {
    const memberError = "You are not a member of this project.";
    const path =
      "/hub/api/users/" + encodeURIComponent(researcher) + "/servers/small";
    const options = {
      headers: {
        "X-XSRFToken": window.jhdata.xsrf_token,
        "Content-Type": "application/json",
      },
      credentials: "same-origin",
      redirect: "error",
      signal: AbortSignal.timeout(120000),
    };
    const response = await fetch(path, {
      ...options,
      method: "POST",
      body: "{}",
    });
    if (response.status === 403)
      return (await response.json()).message === memberError;
    if (response.status !== 202) return false;
    // An asynchronous start reports its final refusal on the Hub's progress stream.
    const progress = await fetch(path + "/progress", options);
    if (!progress.ok) return false;
    const lines = (await progress.text())
      .split("\n")
      .filter((line) => line.startsWith("data:"));
    if (!lines.length) return false;
    const last = JSON.parse(lines.at(-1).slice(5));
    return last.failed === true && last.message.includes(memberError);
  }, user);
  assert.equal(refused, true);
}

async function scenario(browser) {
  stage = "host Chromium DNS and HTTPS trust";
  const preflight = await browser.newPage();
  for (const endpoint of [
    hub + "/hub/login",
    idp + "/realms/heartwood/.well-known/openid-configuration",
  ]) {
    const response = await preflight.goto(endpoint);
    assert(
      response.ok() &&
        new URL(response.url()).origin === new URL(endpoint).origin,
    );
    assert(/^TLS 1[.][23]$/.test((await response.securityDetails())?.protocol));
  }
  await preflight.close();
  stage = stages[0];
  const first = await browser.newContext();
  const second = await browser.newContext();
  const home = await signIn(first, "researcher-one");
  const peerHome = await signIn(second, "researcher-two");
  stage = stages[1];
  const demo = await openProject(first, "demo");
  const other = await openProject(first, "other");
  const peer = await openProject(second, "demo");
  const peerOther = await openProject(second, "other");
  const projects = [demo, other, peer, peerOther];
  assert.equal(peer.user, peerOther.user);
  assert.equal(demo.user, other.user);
  assert.notEqual(demo.user, peer.user);
  assert.equal(new Set(projects.map((server) => server.origin)).size, 4);

  stage = stages[2];
  const title = "Browser verification " + randomUUID();
  await newAnalysis(demo);
  await demo.page.getByRole("button", { name: "Rename session" }).click();
  const edit = demo.page.getByRole("textbox", { name: "Session title" });
  await edit.fill(title);
  await edit.press("Enter");
  await demo.page.getByRole("heading", { name: title }).waitFor();
  for (const target of [other, peer, peerOther]) {
    await target.page.reload();
    await ready(target.page);
    assert.equal(
      await target.page.getByText(title, { exact: true }).count(),
      0,
    );
  }
  for (const from of projects) {
    for (const to of projects) {
      if (from !== to) await cannotRead(from, to);
    }
  }

  stage = stages[3];
  await requireOwnLaunchRefusal(peerHome, peer.user);
  // Both researchers belong to demo. Only the first belongs to small.
  const restricted = await openProject(first, "small");
  for (const target of [demo, restricted]) {
    const denied = await second.newPage();
    const refused = denied.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        response.request().isNavigationRequest() &&
        url.origin === target.origin &&
        url.pathname.endsWith("/oauth_callback")
      );
    });
    await Promise.all([
      denied.goto(target.entry),
      refused.then((response) => assert.equal(response.status(), 403)),
    ]);
    await denied.close();
  }

  stage = stages[4];
  // Opening a second tab from the same project preserves the browser's existing
  // tab session. Its next UI request must lose authorization after logout.
  const [previousLogin] = await Promise.all([
    first.waitForEvent("page"),
    demo.page.evaluate((url) => {
      window.open(url, "_blank");
    }, demo.entry),
  ]);
  await ready(previousLogin);
  await newAnalysis({ page: previousLogin, entry: demo.entry });
  const revoked = demo.page.waitForResponse(
    (response) =>
      response.url() === demo.entry + "logout" &&
      response.request().method() === "POST",
  );
  await Promise.all([
    demo.page.getByRole("button", { name: "Sign out of project" }).click(),
    revoked.then((response) => assert.equal(response.status(), 204)),
  ]);
  await demo.page
    .getByRole("heading", { name: "Signed out of this project" })
    .waitFor();
  const status = await demo.page.evaluate(
    async (url) => (await fetch(url)).status,
    demo.entry + "sessions",
  );
  assert.equal(status, 401);
  const rejected = previousLogin.waitForResponse(
    (response) =>
      response.url() === demo.entry + "sessions" &&
      response.request().method() === "POST",
  );
  await Promise.all([
    previousLogin.getByRole("button", { name: "New analysis" }).click(),
    rejected.then((response) => assert.equal(response.status(), 401)),
  ]);
  await previousLogin.close();
  await newAnalysis(other);
  await newAnalysis(peer);
  await newAnalysis(peerOther);
  const login = demo.page.getByRole("link", { name: "Sign in again" });
  assert.equal(
    new URL(await login.getAttribute("href"), demo.entry).href,
    demo.entry + "identity/login",
  );
  await login.click();
  await ready(demo.page);
  await newAnalysis(demo);

  stage = stages[5];
  await home.locator('a[href$="/hub/logout"]').click();
  await home.goto(hub + "/hub/home");
  await home.waitForURL(
    (url) => url.origin === hub && url.pathname === "/hub/login",
  );
  await peerHome.goto(hub + "/hub/home");
  await peerHome.locator(".new-server-name").waitFor();
  for (const server of projects) await newAnalysis(server);
}

async function browserCheck(browser) {
  const page = await browser.newPage();
  await page.setContent(
    "<button onclick=\"this.textContent='Ready'\">Open</button>",
  );
  await page.getByRole("button", { name: "Open" }).click();
  await page.getByRole("button", { name: "Ready" }).waitFor();
  stage = "browser cross-origin controls";
  const local = http.createServer((_, response) => response.end("fixture"));
  await new Promise((resolve, reject) =>
    local.once("error", reject).listen(0, "127.0.0.1", resolve),
  );
  try {
    const entry = "http://127.0.0.1:" + local.address().port + "/";
    await page.goto(entry);
    // A readable response on the same origin must fail the refusal assertion.
    await assert.rejects(cannotRead({ page }, { entry }), {
      name: "AssertionError",
    });
    // localhost and 127.0.0.1 reach the same server with different origins.
    await cannotRead(
      { page },
      { entry: entry.replace("127.0.0.1", "localhost") },
    );
  } finally {
    local.closeAllConnections();
    await new Promise((resolve) => local.close(resolve));
  }
  // A fresh, untrusted local CA must fail in this Chromium configuration.
  stage = "untrusted localhost certificate control";
  const key = execFileSync("openssl", ["genrsa", "2048"], {
    stdio: ["ignore", "pipe", "ignore"],
  });
  const cert = execFileSync(
    "/bin/sh",
    [
      "-c",
      "cat | openssl req -x509 -new -key /dev/stdin -subj /CN=localhost -addext subjectAltName=IP:127.0.0.1 -days 1",
    ],
    { input: key, stdio: ["pipe", "pipe", "ignore"] },
  );
  const server = https.createServer({ key, cert }, (_, res) =>
    res.end("fixture"),
  );
  await new Promise((resolve, reject) =>
    server.once("error", reject).listen(0, "127.0.0.1", resolve),
  );
  try {
    await assert.rejects(
      page.goto("https://127.0.0.1:" + server.address().port),
      (error) => error.message.includes("ERR_CERT_AUTHORITY_INVALID"),
    );
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

async function main() {
  const mode = process.argv[2];
  if (mode === "--list") return console.log(stages.join("\n"));
  if (!["--browser-check", "--run"].includes(mode))
    throw new Error("choose a mode");
  for (const key of [
    "DEBUG",
    "PWDEBUG",
    "SSLKEYLOGFILE",
    "NODE_DEBUG",
    "NODE_OPTIONS",
  ]) {
    if (process.env[key]) throw new Error("diagnostic recording enabled");
  }
  const { chromium } = require("playwright");
  const proxy = process.env.HEARTWOOD_BROWSER_PROXY;
  const expectedProxy =
    disposableLinux ?
      /^http:\/\/host\.docker\.internal:[0-9]+$/
    : /^http:\/\/127\.0\.0\.1:[0-9]+$/;
  if (proxy && !expectedProxy.test(proxy))
    throw new Error("expected loopback proxy");
  const browser = await chromium.launch({
    ...(disposableLinux ?
      {
        executablePath:
          "/ms-playwright/chromium-1255/chrome-linux-arm64/chrome",
      }
    : { channel: "chrome" }),
    chromiumSandbox: true,
    ...(mode === "--run" && proxy ? { proxy: { server: proxy } } : {}),
  });
  try {
    if (mode === "--browser-check") await browserCheck(browser);
    else await scenario(browser);
    console.log("PASS", mode, "Chromium", browser.version());
  } finally {
    await browser.close();
  }
}
main().catch(() => {
  // Playwright exceptions can include navigated URLs or action arguments.
  console.error("FAIL:", stage);
  process.exitCode = 1;
});
