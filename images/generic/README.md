<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Generic Heartwood Runtime

This directory defines the generic runtime.
See [Containers](../../documentation/platforms/containers.md) for the user workflow, [Testing and Evidence](../../documentation/architecture/testing.md) for validation language, and [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) for planned work.

The generic image packages the Heartwood CLI, gateway, notebook bridge, web UI, OpenHands SDK and tools, repository-verified Skills, policy and audit stack, synthetic fixtures, the Heartwood-managed model recommendation and Hugging Face planning contract, and pinned `llama-server` binaries for `linux/amd64` and `linux/arm64`.

The image contains no model weights, provider credentials, generated model profiles, or separate OpenHands agent server. OpenHands runs in process behind the Heartwood backend contract. Mount one project at `/workspace`; Heartwood stores optional recommended or user-selected models, configuration, sessions, Skills, logs, and audit records in that project's `.heartwood/` directory. A download starts only after an explicit CLI or web request.

`images/generic/compose.yaml` builds the production runtime target, disables container networking, uses a read-only root filesystem and restricted Linux privileges, starts the deterministic loopback model fixture, configures a non-secret managed profile, completes OpenHands SDK conversations in both action-confirmation modes, loads all repository-verified Skills through OpenHands, and exports a scrubbed audit log. The fixture validates integration only and is not a model artifact.

`images/generic/scripts/coding_agent_e2e.sh` is the shared coding-agent acceptance contract. It requires direct model inference, a native OpenHands terminal proposal, a displayed grouped approval, successful execution after explicit approval, exact synthetic file content, fresh-process replay, and hash-chain-verified audit export. `images/generic/scripts/capable_model_e2e.sh` applies that contract to an explicitly downloaded, read-only GGUF mount while container networking is disabled. The GPU wrapper applies the same contract to one immutable safetensors snapshot and records the runtime, GPU, driver, context, tensor parallelism, and parser in its qualification report.

The recommended Qwen2.5 7B Instruct GGUF artifact remains the portable CPU tool-use demonstration. CUDA configurations are recommended only after the complete acceptance contract passes on the named GPU platform.

Run `heartwood --interface web --host 0.0.0.0` for the browser interface. Heartwood uses the configured hosted or managed connection directly, or starts the selected downloaded model before opening the same interface. `heartwood runtime start` and `heartwood gateway serve` remain operator commands; the lower-level runtime scripts are test fixtures, not a separate researcher setup contract.

Public generic tags are `edge` and `sha-<git-sha>`. Native architecture builds are staged without tags and tested by digest before their validated descriptors are merged into the immutable commit tag; `edge` is moved to that verified manifest last. No persistent architecture helper tags are created.
