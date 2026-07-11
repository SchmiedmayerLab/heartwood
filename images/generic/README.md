<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Generic Heartwood Runtime

This directory defines the implemented generic runtime. Current platform and validation status is recorded in [Platform Support](../../docs/platform-support.md); future image work is recorded in the [Delivery Roadmap](../../design/09-implementation-plan.md).

The generic image packages the Heartwood CLI, gateway, notebook bridge, web UI, OpenHands SDK and tools, repository-verified Skills, policy and audit stack, synthetic fixtures, reviewed local-artifact metadata, and pinned `llama-server` binaries for `linux/amd64` and `linux/arm64`.

The image contains no model weights, provider credentials, generated model profiles, or separate OpenHands agent server. OpenHands runs in process behind the Heartwood backend contract. Runtime profiles point either to a policy-authorized provider or to an OpenAI-compatible local service. Optional reviewed artifacts are downloaded only after an explicit CLI or web request and are stored under `HEARTWOOD_MODEL_CACHE`, which should be a mounted volume. `HF_HOME` defaults to a directory within that volume so resumable Hugging Face and Xet transfer state remains writable in a read-only container; override both variables together for a different mount.

`images/generic/compose.yaml` builds the production runtime target, disables container networking, uses a read-only root filesystem and restricted Linux privileges, starts the deterministic loopback model fixture, configures a non-secret local profile, completes OpenHands SDK conversations in both action-confirmation modes, loads all repository-verified Skills through OpenHands, and exports a scrubbed audit log. The fixture validates integration only and is not a model artifact.

`images/generic/scripts/capable_model_e2e.sh` is the separate resource-qualified inference acceptance path. It consumes an explicitly downloaded, read-only GGUF mount, starts the included llama.cpp runtime, selects **Auto-Approve Low Risk**, and requires a native OpenHands tool proposal, successful terminal execution, exact synthetic file content, route and action-mode records, no error events, and audit export while container networking is disabled. The reviewed Qwen2.5 7B Instruct artifact is the default tool-use demonstration model; the Coder variant remains available for coding-output experiments but is not used to claim structured agent-tool compatibility.

`images/generic/scripts/start_demo_stack.sh` serves the web UI without starting a model by default. Set `HEARTWOOD_DEMO_START_LOCAL_RUNTIME=1` and provide an explicit `HEARTWOOD_LOCAL_MODEL_PATH` to start the included CPU `llama-server`; otherwise configure an existing local or hosted endpoint through the web panel or `heartwood models` commands.

Public generic tags are `edge` and `sha-<git-sha>`. Architecture builds are pushed by digest and merged into these multi-platform tags without persistent helper tags.
