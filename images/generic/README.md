<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Generic Heartwood Runtime

This directory defines the implemented generic runtime. Current platform and validation status is recorded in [Platform Support](../../docs/platform-support.md); planned image work is tracked in [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues).

The generic image packages the Heartwood CLI, gateway, notebook bridge, web UI, OpenHands SDK and tools, repository-verified Skills, policy and audit stack, synthetic fixtures, reviewed local-artifact metadata, and pinned `llama-server` binaries for `linux/amd64` and `linux/arm64`.

The image contains no model weights, provider credentials, generated model profiles, or separate OpenHands agent server. OpenHands runs in process behind the Heartwood backend contract. Mount one project at `/workspace`; Heartwood stores optional reviewed artifacts, configuration, sessions, Skills, logs, and audit records in that project's `.heartwood/` directory. A download starts only after an explicit CLI or web request.

`images/generic/compose.yaml` builds the production runtime target, disables container networking, uses a read-only root filesystem and restricted Linux privileges, starts the deterministic loopback model fixture, configures a non-secret local profile, completes OpenHands SDK conversations in both action-confirmation modes, loads all repository-verified Skills through OpenHands, and exports a scrubbed audit log. The fixture validates integration only and is not a model artifact.

`images/generic/scripts/capable_model_e2e.sh` is the separate resource-qualified inference acceptance path. It consumes an explicitly downloaded, read-only GGUF mount, starts the included llama.cpp runtime, selects **Auto-Approve Low Risk**, and requires a native OpenHands tool proposal, successful terminal execution, exact synthetic file content, route and action-mode records, no error events, and audit export while container networking is disabled. The reviewed Qwen2.5 7B Instruct artifact is the default tool-use demonstration model; the Coder variant remains available for coding-output experiments but is not used to claim structured agent-tool compatibility.

Run `heartwood serve --host 0.0.0.0` for the web interface with a hosted or already running model service. After `heartwood models download <artifact-id>`, run `heartwood launch --web --host 0.0.0.0` to let Heartwood supervise the packaged CPU `llama-server`. The lower-level runtime scripts are test fixtures, not a separate researcher setup contract.

Public generic tags are `edge` and `sha-<git-sha>`. Native architecture builds are staged without tags and tested by digest before their validated descriptors are merged into the immutable commit tag; `edge` is moved to that verified manifest last. No persistent architecture helper tags are created.
