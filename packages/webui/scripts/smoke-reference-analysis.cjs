/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium, expect } = require("@playwright/test");

const packageRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(packageRoot, "../..");
const heartwoodExecutable = path.join(repoRoot, ".venv", "bin", "heartwood");
const pythonExecutable = path.join(repoRoot, ".venv", "bin", "python");
const webRoot = path.join(packageRoot, "dist");
const stateRoot = fs.mkdtempSync(
  path.join(os.tmpdir(), "heartwood-reference-analysis-"),
);
const gatewayPort = process.env.HEARTWOOD_REFERENCE_GATEWAY_PORT || "4187";
const origin = `http://127.0.0.1:${gatewayPort}`;
const screenshotOption =
  process.env.HEARTWOOD_REFERENCE_SCREENSHOT_DIR ||
  optionValue("--screenshot-dir");
const screenshotDirectory =
  screenshotOption === null ? null : (
    path.resolve(process.cwd(), screenshotOption)
  );
const desktopViewport = { height: 1024, width: 1440 };
const cohortPrompt =
  "Build the synthetic target-condition cohort for concept 201826 with the " +
  "repository-verified cohort Skill. Use the localized OMOP reference tables, " +
  "minimum age 18, aggregate count floor 20, and write cohort-summary.json. " +
  "Report the cohort definition and quality checks without row-level values.";
const baselinePrompt =
  "Fit the repository-verified training-only age-only baseline for recorded " +
  "condition 201826 history in the synthetic OMOP tables. Write " +
  "baseline-model.json and report aggregate training diagnostics, the lack of " +
  "holdout evaluation, and that this is not a clinical model.";
const exportPrompt =
  "Prepare the aggregate export from cohort-summary.json with the " +
  "repository-verified aggregate export Skill. Apply a count floor of 20, " +
  "write aggregate-export.json, and do not include row-level values.";
const failurePrompt =
  "Run the failing-action integration check and report the terminal failure " +
  "without changing the completed reference artifacts.";
const processes = [];
const logs = [];
const runtimeEnvironment = Object.assign({}, process.env, {
  HEARTWOOD_MODEL_REQUEST_LOG: path.join(stateRoot, "model-requests.jsonl"),
  HEARTWOOD_RUNTIME_ROOT: repoRoot,
  HEARTWOOD_TOOL_PYTHON: "python",
  LITELLM_LOCAL_MODEL_COST_MAP: "True",
  OPENHANDS_SUPPRESS_BANNER: "1",
  PATH: `${path.dirname(pythonExecutable)}${path.delimiter}${process.env.PATH || ""}`,
  UV_CACHE_DIR: path.join(repoRoot, ".uv-cache"),
});

main().catch((error) => {
  console.error(error);
  if (logs.length) console.error(logs.join(""));
  process.exitCode = 1;
});

async function main() {
  if (!fs.existsSync(path.join(webRoot, "index.html"))) {
    throw new Error("web UI assets are missing; run npm run build first");
  }
  if (!fs.existsSync(heartwoodExecutable) || !fs.existsSync(pythonExecutable)) {
    throw new Error(
      "Python workspace is missing; run uv sync --locked --all-extras",
    );
  }

  let browser;
  try {
    const inputRoot = path.join(stateRoot, "input");
    fs.mkdirSync(inputRoot, { recursive: true });
    for (const filename of ["person.csv", "condition_occurrence.csv"]) {
      fs.copyFileSync(
        path.join(repoRoot, "fixtures", "synthetic", "omop-like", filename),
        path.join(inputRoot, filename),
      );
    }
    startProcess(pythonExecutable, [
      path.join(repoRoot, "images/generic/scripts/local_model_stub.py"),
      "--host",
      "127.0.0.1",
      "--port",
      "8765",
      "--request-log",
      runtimeEnvironment.HEARTWOOD_MODEL_REQUEST_LOG,
    ]);
    await waitForUrl("http://127.0.0.1:8765/v1/models");
    runCli("models", "refresh", "heartwood");
    runCli("models", "connect", "heartwood", "heartwood-managed-runtime");

    startProcess(heartwoodExecutable, [
      "--interface",
      "web",
      "--host",
      "127.0.0.1",
      "--port",
      gatewayPort,
    ]);
    await waitForUrl(origin);

    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({
      acceptDownloads: true,
      viewport: desktopViewport,
    });
    await page.goto(origin);

    const task = page.getByRole("textbox", { name: "Task", exact: true });
    await expect(task).toBeEnabled({ timeout: 30_000 });
    const sessions = await fetchJson(`${origin}/sessions`);
    const session = sessions.sessions?.[0];
    if (!session || typeof session.session_id !== "string") {
      throw new Error(
        "real browser workflow did not create a persisted session",
      );
    }
    const sessionId = session.session_id;
    await page.getByLabel("Rename session").click();
    await page.getByLabel("Session title").fill("Synthetic Cohort Analysis");
    await page.getByLabel("Session title").press("Enter");
    await expect(
      page.getByRole("heading", { name: "Synthetic Cohort Analysis" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Skills", exact: true }).click();
    await expect(
      page.getByText("omop-cohort-summary", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("baseline-model", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("aggregate-export", { exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Close", exact: true }).click();

    await runApprovedTask(page, task, {
      captureApproval: true,
      finalMessage:
        "The synthetic target-condition cohort summary is ready for review.",
      prompt: cohortPrompt,
      summary: "build the aggregate synthetic target-condition cohort",
    });
    await captureReferenceScreenshots(page);
    await runApprovedTask(page, task, {
      finalMessage: "The training-only age baseline is ready for review.",
      prompt: baselinePrompt,
      summary: "fit the training-only synthetic age baseline",
    });
    await runApprovedTask(page, task, {
      finalMessage:
        "The count-floor-controlled aggregate export is ready for review.",
      prompt: exportPrompt,
      summary: "apply the aggregate count floor and prepare the export",
    });
    await runApprovedTask(page, task, {
      finalMessage:
        "The requested tool action failed; review the terminal outcome before retrying.",
      prompt: failurePrompt,
      summary: "run the failing synthetic command",
    });

    const artifactPath = path.join(stateRoot, "cohort-summary.json");
    if (!fs.existsSync(artifactPath)) {
      const events = await fetchJson(`${origin}/sessions/${sessionId}/events`);
      const files = listFiles(stateRoot);
      throw new Error(
        `reference artifact is missing at ${artifactPath}\n` +
          `persisted files: ${JSON.stringify(files)}\n` +
          `session events: ${JSON.stringify(events)}`,
      );
    }
    const cohort = JSON.parse(fs.readFileSync(artifactPath, "utf8"));
    if (
      cohort.summary?.source_participant_count !== 24 ||
      cohort.summary?.source_condition_occurrence_count !== 39 ||
      cohort.summary?.participant_count !== 20 ||
      cohort.summary?.condition_occurrence_count !== 35 ||
      cohort.export_guard?.exportable !== true
    ) {
      throw new Error(
        `unexpected browser-generated cohort: ${JSON.stringify(cohort)}`,
      );
    }
    if (cohort.quality_checks?.aggregate_only_output !== true) {
      throw new Error(
        "browser-generated cohort did not preserve aggregate-only output",
      );
    }
    const baseline = readJsonArtifact(stateRoot, "baseline-model.json");
    if (
      baseline.model?.model_type !== "synthetic-logistic-condition-history" ||
      baseline.training_summary?.participant_count !== 24 ||
      baseline.training_summary?.positive_count !== 20 ||
      baseline.quality_checks?.holdout_evaluation_performed !== false ||
      baseline.quality_checks?.aggregate_only_output !== true
    ) {
      throw new Error(
        `unexpected browser-generated baseline: ${JSON.stringify(baseline)}`,
      );
    }
    const aggregate = readJsonArtifact(stateRoot, "aggregate-export.json");
    if (
      aggregate.exported !== true ||
      aggregate.suppressed !== false ||
      aggregate.aggregate_count_floor !== 20 ||
      aggregate.aggregates?.participant_count !== 20 ||
      aggregate.aggregates?.target_condition_occurrence_count !== 20
    ) {
      throw new Error(
        `unexpected browser-generated export: ${JSON.stringify(aggregate)}`,
      );
    }

    await page
      .getByRole("button", { name: "Activity & audit", exact: true })
      .click();
    await expect(page.getByText("Tool execution", { exact: true })).toHaveCount(
      4,
    );
    await expect(page.getByText("exit=0", { exact: true })).toHaveCount(3);
    await expect(page.getByText("exit=1", { exact: true })).toHaveCount(1);
    await page.getByRole("button", { name: "Close", exact: true }).click();

    const downloadPromise = page.waitForEvent("download");
    await page
      .getByRole("button", { name: "Export audit", exact: true })
      .click();
    const download = await downloadPromise;
    if (download.suggestedFilename() !== `${sessionId}-audit.jsonl`) {
      throw new Error(
        `unexpected audit filename: ${download.suggestedFilename()}`,
      );
    }
    const downloadPath = await download.path();
    if (downloadPath === null)
      throw new Error("browser audit download has no local path");
    const audit = fs.readFileSync(downloadPath, "utf8");
    if (
      !audit.includes("audit.export.recorded") ||
      [cohortPrompt, baselinePrompt, exportPrompt, failurePrompt].some(
        (prompt) => audit.includes(prompt),
      )
    ) {
      throw new Error(
        "browser audit export is incomplete or contains prompt content",
      );
    }

    const replay = runCli("--session-id", sessionId, "replay");
    if (
      !replay.includes("Action set approved") ||
      replay.match(/Tool terminal exit=0/gu)?.length !== 3 ||
      replay.match(/Tool terminal exit=1/gu)?.length !== 1 ||
      !replay.includes(
        "Agent: The synthetic target-condition cohort summary is ready for review.",
      ) ||
      !replay.includes(
        "Agent: The training-only age baseline is ready for review.",
      ) ||
      !replay.includes(
        "Agent: The count-floor-controlled aggregate export is ready for review.",
      ) ||
      !replay.includes(
        "Agent: The requested tool action failed; review the terminal outcome before retrying.",
      )
    ) {
      throw new Error(`CLI did not replay the browser session:\n${replay}`);
    }
    console.log("Reference analysis browser and CLI system test: ok");
  } finally {
    if (browser) await browser.close();
    for (const child of processes.reverse()) terminateProcessGroup(child);
    await Promise.all(processes.map(waitForExit));
    if (process.env.HEARTWOOD_REFERENCE_PRESERVE_STATE !== "1") {
      fs.rmSync(stateRoot, { force: true, recursive: true });
    } else {
      console.error(`Preserved reference-analysis state at ${stateRoot}`);
    }
  }
}

async function runApprovedTask(page, task, taskSpec) {
  await task.fill(taskSpec.prompt);
  await task.press("Enter");
  const approval = page
    .getByRole("region", { name: "Approval required for OpenHands action set" })
    .last();
  await expect(approval).toBeVisible({ timeout: 60_000 });
  await expect(
    approval.getByText(taskSpec.summary, { exact: true }),
  ).toBeVisible();
  if (taskSpec.captureApproval === true) {
    await captureDesktopScreenshot(
      page,
      "browser-action-review.png",
      "action review",
    );
  }
  await approval
    .getByRole("button", { name: /^Allow all \d+ actions? once$/u })
    .click();
  await expect(
    page.getByText(taskSpec.finalMessage, { exact: true }),
  ).toBeVisible({ timeout: 60_000 });
}

async function captureReferenceScreenshots(page) {
  if (screenshotDirectory === null) return;
  await page
    .getByRole("log", { name: "Conversation transcript" })
    .evaluate((element) => {
      element.scrollTop = 0;
    });
  await captureDesktopScreenshot(
    page,
    "browser-conversation.png",
    "conversation",
  );
}

async function captureDesktopScreenshot(page, filename, stateName) {
  if (screenshotDirectory === null) return;
  fs.mkdirSync(screenshotDirectory, { recursive: true });
  const screenshotPath = path.join(screenshotDirectory, filename);
  await assertNoHorizontalOverflow(page, stateName);
  await page.screenshot({ path: screenshotPath });
  if (fs.statSync(screenshotPath).size < 1_000) {
    throw new Error(
      `reference screenshot is unexpectedly small: ${screenshotPath}`,
    );
  }
}

async function assertNoHorizontalOverflow(page, viewportName) {
  const dimensions = await page.evaluate(() => {
    const root = globalThis.document.documentElement;
    return {
      clientWidth: root.clientWidth,
      scrollWidth: root.scrollWidth,
    };
  });
  if (dimensions.scrollWidth > dimensions.clientWidth) {
    throw new Error(
      `${viewportName} viewport overflows horizontally: ${JSON.stringify(dimensions)}`,
    );
  }
}

function readJsonArtifact(root, filename) {
  const artifact = path.join(root, filename);
  if (!fs.existsSync(artifact)) {
    throw new Error(`reference artifact is missing at ${artifact}`);
  }
  return JSON.parse(fs.readFileSync(artifact, "utf8"));
}

function startProcess(command, args) {
  const child = spawn(command, args, {
    cwd: stateRoot,
    detached: true,
    env: runtimeEnvironment,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (chunk) => logs.push(String(chunk)));
  child.stderr.on("data", (chunk) => logs.push(String(chunk)));
  processes.push(child);
  return child;
}

function runCli(...args) {
  const result = spawnSync(heartwoodExecutable, args, {
    cwd: stateRoot,
    encoding: "utf8",
    env: runtimeEnvironment,
  });
  if (result.status !== 0) {
    throw new Error(
      `heartwood ${args.join(" ")} failed:\n${result.stdout}${result.stderr}`,
    );
  }
  return result.stdout;
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

async function waitForUrl(url) {
  const deadline = Date.now() + 60_000;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
      lastError = new Error(`${url} returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`${url} did not become ready: ${String(lastError)}`);
}

function terminateProcessGroup(child) {
  if (child.exitCode !== null || child.pid === undefined) return;
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch (error) {
    if (error.code !== "ESRCH") throw error;
  }
}

async function waitForExit(child) {
  if (child.exitCode !== null) return;
  await new Promise((resolve) => child.once("exit", resolve));
}

function listFiles(root) {
  const files = [];
  const pending = [root];
  while (pending.length) {
    const current = pending.pop();
    if (!current || !fs.existsSync(current)) continue;
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const location = path.join(current, entry.name);
      if (entry.isDirectory()) pending.push(location);
      else files.push(path.relative(root, location));
    }
  }
  return files.sort();
}

function optionValue(name) {
  const index = process.argv.indexOf(name);
  if (index === -1) return null;
  const value = process.argv[index + 1];
  if (!value || value.startsWith("--")) {
    throw new Error(`${name} requires a directory`);
  }
  return value;
}
