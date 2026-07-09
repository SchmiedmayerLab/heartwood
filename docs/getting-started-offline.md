<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Getting Started With The Offline Stack

This guide runs the Phase 0 generic Heartwood stack without public runtime network access. It demonstrates the intended shipping shape: one multi-architecture container image with the CLI, gateway-backed session flow, notebook package, synthetic fixtures, verified skills, policy-gated local model path, audit export, and a synthetic evidence bundle.

The default local-runtime profile in this guide is `stub-loopback`, a deterministic loopback stub. It is not an LLM inference runtime, does not include model weights, is not a production model, and does not make a quality claim. Its purpose is to prove that Heartwood can invoke an allowlisted in-boundary model endpoint through the same agentic CLI path while the container runtime has no external network.

## Local Inference Scope

The current smoke test proves the offline integration path, not real LLM inference. It exercises the CLI, gateway-backed session contract, policy allowlist, approval record, loopback HTTP model endpoint, scrubbed audit export, and evidence-bundle generation. The checked-in local-runtime manifest selects `llama-cpp-cpu` as the first real local inference profile and defines the remaining runtime, model artifact, checksum, license, resource, architecture, and CI requirements for loading and querying a real model without public network access.

The minimal CI target for real inference is a tiny same-runtime GGUF artifact served through `llama-cpp-python` on CPU, loaded from the built image or a verified local artifact path, queried through the gateway after explicit approval, and run with runtime network disabled. Pull-request CI should keep that artifact small and deterministic; the local tutorial can use a more useful small open-weight model once its license, redistribution allowance, checksum, and resource envelope are recorded. A good local demonstration candidate is a 0.5B to 1B Apache-2.0 GGUF chat model, such as a Qwen3 0.6B quantized artifact after the exact file, publisher, license, and checksum are pinned.

## Architecture And Acceleration Scope

The generic image is expected to publish and smoke-test `linux/amd64` and `linux/arm64` variants. The current technology stack is Python-first and uses portable container tooling, so both architectures are reasonable baseline targets. GitHub-hosted CI uses standard `ubuntu-latest` runners with QEMU for the `linux/arm64` smoke path until native hosted `arm64` runners are stable enough for this repository's required checks.

Docker can expose GPUs to containers, but GPU support is a host capability, not something a portable CPU image can guarantee. The baseline real runtime therefore remains `llama-cpp-cpu`; NVIDIA acceleration is tracked separately as `llama-cpp-cuda`, requiring an explicit CUDA-enabled runtime, Docker GPU device exposure, and self-hosted GPU CI or scheduled platform checks. The CPU profile must keep working without a GPU.

## Agent-Server Scope

The generic image includes the Heartwood gateway and backend facade, not a pinned production OpenHands runtime. Current tests cover the managed localhost-only process boundary and fake OpenHands-style event translation through the session contract. A later runtime profile must bundle a pinned OpenHands agent-server command, start it behind the gateway in the image, and run a CLI-gateway-agent-server smoke test without public network access.

## Run From The Published Image

After the main-branch image is published, run the offline smoke workflow with Docker only:

```bash
docker pull ghcr.io/schmiedmayerlab/heartwood:dev-main
docker run --rm --network none ghcr.io/schmiedmayerlab/heartwood:dev-main bash images/generic/scripts/offline_stack_smoke.sh
```

The command starts the `stub-loopback` runtime profile, runs detection, approves the synthetic model call, invokes `heartwood run --local-model`, exports a scrubbed audit JSONL file, and writes the synthetic evidence bundle under `/tmp/heartwood-reviewer-packet`.

## Run From A Checkout

From a repository checkout, run the same smoke test through Docker Compose:

```bash
docker compose -f images/generic/compose.yaml run --rm heartwood
```

Compose builds the local image, disables runtime network access with `network_mode: none`, and runs the same offline smoke script used by CI.

## What The Smoke Test Proves

- The CLI can drive the gateway-backed session contract inside the image.
- The generic policy allows only configured model endpoints and includes the loopback stub endpoint.
- The loopback stub call happens over `127.0.0.1` while external network is disabled.
- The model response content is not persisted into session events or audit exports; only response metadata is recorded.
- The deterministic backend still emits tool proposal, confirmation, and execution events after the model call.
- The audit export and reviewer packet can be produced from the same offline session.

## What It Does Not Prove Yet

- It does not validate controlled data.
- It does not yet include the selected `llama-cpp-cpu` runtime dependency or model weights.
- It does not yet run the real local-runtime profile defined in `images/generic/local-runtime/profiles.toml`.
- It does not yet run a tiny same-runtime GGUF artifact in pull-request CI.
- It does not yet validate optional GPU acceleration; that belongs to a separate CUDA profile and a GPU-capable runner.
- It does not launch a production OpenHands agent-server behind the gateway.
- It does not run the researcher web UI or platform proxy routes.
- It does not publish the static documentation site; that belongs with the web/documentation pass.
