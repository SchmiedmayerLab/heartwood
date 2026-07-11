<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Researcher Web Interface

The Heartwood web interface is a conversation-first view of the same persisted sessions, model settings, OpenHands actions, Skills, and audit events exposed by the CLI and notebook bridge. It does not implement a separate agent loop or browser-only workflow state.

## Start The Interface

For a source checkout, complete [Local Development](../README.md#local-development), build the web assets, and start the gateway:

```bash
cd packages/webui
npm ci
npm run build
cd ../..
uv run heartwood serve --web-root packages/webui/dist
```

Open `http://127.0.0.1:8767/`. Container users should follow [Container Images](container-images.md); Terra users should follow the [Terra Jupyter Demo](terra-jupyter-demo.md) and open the authenticated notebook proxy route rather than exposing the gateway directly.

## Configure A Model

Open **Settings** and select one connection:

- **Local** lists models reported by the configured local OpenAI-compatible runtime. Reviewed artifact downloads show transferred bytes and integrity status, but the corresponding inference runtime must also be running.
- **Research environment** lists platform-provided model connections and exact identifiers supplied by deployment configuration.
- **OpenAI** and **Anthropic** discover exact identifiers through the providers' maintained APIs after a token is supplied.
- **Custom API** connects to another OpenAI-compatible endpoint and uses its model catalog when available.

The selected connection must have an available credential reference and an allowed deployment-policy route before the composer is enabled. A route decision is not evidence that the provider is compliant, reachable, or capable; see [Model Connections](model-connections.md) for the full contract.

## Run The Synthetic Reference Analysis

Create a session, select **Detect environment**, and submit the three tasks from the [Terra Jupyter Demo](terra-jupyter-demo.md#start-the-web-interface). Review each proposed action before selecting **Allow once**. The reference sequence builds the adult target-condition cohort, fits the explicitly training-only age baseline, and prepares the count-floor-controlled aggregate export.

![Heartwood synthetic reference analysis showing the cohort, baseline, and aggregate-export conversation](assets/web-reference-analysis.png)

This screenshot is produced by the real reference-analysis system test using the production web build, gateway, OpenHands SDK adapter, repository-verified Skills, persisted session store, and deterministic loopback model fixture. It validates orchestration and interface parity, not model quality or live Terra behavior.

## Review Actions And Evidence

**Ask Every Time** requires an explicit decision for every OpenHands action. **Auto-Approve Low Risk** delegates risk analysis and confirmation to OpenHands: low-risk actions continue automatically, while medium-, high-, and unknown-risk actions still require **Allow once** or **Reject**. The web interface never exposes an unconditional automatic mode.

Open **Activity & audit** to inspect ordered route decisions, action proposals, confirmations, tool outcomes, and errors. **Export audit** downloads a content-minimized JSON Lines record that excludes prompts, model responses, action summaries, paths, row values, and secrets. Successful tool execution does not authorize data export outside the workspace.

## Resume The Same Session From The CLI

The session rail title and CLI session identifier refer to the same gateway-owned record. From another terminal using the same workspace root:

```bash
heartwood --workspace <session-root> sessions list
heartwood --workspace <session-root> --session-id <session-id> replay
heartwood --workspace <session-root> --session-id <session-id> chat
```

Use one active writer per file-backed session. Run web, CLI, and notebook commands sequentially after the current agent turn is idle.

## Use A Notebook Viewport

The compact layout preserves the session title, model status, detected platform and dataset, approval mode, transcript, and composer when opened through a narrow Jupyter proxy viewport. The session rail moves behind the menu button; Skills, Activity, Settings, and audit export remain available from the same navigation.

![Heartwood synthetic reference analysis at a narrow notebook viewport](assets/web-notebook-viewport.png)

Proxy authentication, prefix rewriting, and WebSocket behavior are platform contracts tested separately from responsive layout. The screenshot does not represent a live Terra control-plane validation.

## Regenerate The Screenshots

The screenshot command runs the complete synthetic browser-to-gateway-to-OpenHands workflow, validates all three artifacts, checks the failed-action path and scrubbed audit download, replays the browser-created session through the CLI, and writes desktop and notebook-viewport images:

```bash
uv sync --locked --all-extras
cd packages/webui
npm ci
npx playwright install chromium
npm run build
npm run screenshots:docs
```

CI invokes the same system test with a temporary screenshot directory. Repository screenshots must contain only synthetic data and must be regenerated whenever the documented interface changes materially.
