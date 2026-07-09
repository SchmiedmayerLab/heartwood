<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Getting Started With The Offline Stack

This guide runs the Phase 0 generic Heartwood stack without public runtime network access. It demonstrates the intended shipping shape: one container image with the CLI, gateway-backed session flow, notebook package, synthetic fixtures, verified skills, policy-gated local model path, audit export, and reviewer packet generation.

The local model in this guide is a tiny deterministic loopback stub. It is not an LLM inference runtime, does not include model weights, is not a production model, and does not make a quality claim. Its purpose is to prove that Heartwood can invoke an allowlisted in-boundary model endpoint through the same agentic CLI path while the container runtime has no external network.

## Local Inference Scope

Phase 0F proves the offline integration path, not real LLM inference. The smoke test exercises the CLI, gateway-backed session contract, policy allowlist, approval record, loopback HTTP model endpoint, scrubbed audit export, and reviewer packet generation. A later local-inference profile must add a selected inference runtime, a model artifact strategy, artifact hash verification, license review, resource limits, and tests that prove a real model can be loaded and queried without public network access.

## Agent-Server Scope

The generic image includes the Heartwood gateway and backend facade, not a pinned production OpenHands runtime. Current tests cover the managed localhost-only process boundary and fake OpenHands-style event translation through the session contract. A later runtime profile must bundle a pinned OpenHands agent-server command, start it behind the gateway in the image, and run a CLI-gateway-agent-server smoke test without public network access.

## Run From The Published Image

After the main-branch image is published, run the offline smoke workflow with Docker only:

```bash
docker pull ghcr.io/schmiedmayerlab/heartwood:dev-main
docker run --rm --network none ghcr.io/schmiedmayerlab/heartwood:dev-main bash images/generic/scripts/offline_stack_smoke.sh
```

The command starts a loopback model stub, runs detection, approves the synthetic model call, invokes `heartwood run --local-model`, exports a scrubbed audit JSONL file, and writes a reviewer packet under `/tmp/heartwood-reviewer-packet`.

## Run From A Checkout

From a repository checkout, run the same smoke test through Docker Compose:

```bash
docker compose -f images/generic/compose.yaml run --rm heartwood
```

Compose builds the local image, disables runtime network access with `network_mode: none`, and runs the same offline smoke script used by CI.

## What The Smoke Test Proves

- The CLI can drive the gateway-backed session contract inside the image.
- The generic policy allows only configured model endpoints and includes the loopback model-stub endpoint.
- The loopback model-stub call happens over `127.0.0.1` while external network is disabled.
- The model response content is not persisted into session events or audit exports; only response metadata is recorded.
- The deterministic backend still emits tool proposal, confirmation, and execution events after the model call.
- The audit export and reviewer packet can be produced from the same offline session.

## What It Does Not Prove Yet

- It does not validate controlled data.
- It does not include an LLM inference mechanism or model weights.
- It does not use a production local model runtime.
- It does not launch a production OpenHands agent-server behind the gateway.
- It does not run the researcher web UI or platform proxy routes.
- It does not publish the static documentation site; that belongs with the web/documentation pass.
