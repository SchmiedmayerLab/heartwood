<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Getting Started With Local And Offline Models

This guide configures Heartwood with either an existing local OpenAI-compatible service or an explicitly downloaded reviewed artifact. Heartwood images contain the inference runtime and artifact metadata but no model weights.

This is current operational documentation for the generic runtime. Current platform status is recorded in [Platform Support](platform-support.md), design rationale is recorded in [03 — Architecture](../design/03-architecture.md), and unimplemented work is recorded in the [Delivery Roadmap](../design/09-implementation-plan.md).

## Validate The No-Weight Integration

From a repository checkout:

```bash
docker compose -f images/generic/compose.yaml run --rm --build heartwood
```

Compose disables container networking and uses a read-only root filesystem, dropped capabilities, `no-new-privileges`, and temporary write points. Inside that boundary the smoke test starts the deterministic loopback model fixture, discovers and selects its reported model through the shared catalog, runs an OpenHands SDK conversation, exercises both action-confirmation modes, loads the repository-verified Skills through OpenHands, records route and action events, and exports a scrubbed audit log. This proves the no-network catalog, orchestration, policy, approval, Skill, audit, CLI, web-support, and notebook contracts without claiming real model inference. The mounted capable-model test below is the separate inference and tool-execution gate.

## Use An Existing Local Service

Start Ollama, vLLM, SGLang, llama.cpp, or another service on `127.0.0.1:8765` that provides OpenAI-compatible model-list and chat-completions routes. Then inspect and select an exact reported identifier:

```bash
heartwood models refresh local
heartwood models connect local <model-id>
heartwood chat
```

The Local connection is fixed to loopback and requires no credential. For a service on another port or host, use Custom API with the credential and deployment-policy controls appropriate to that route. Remote custom services require HTTPS and a token. See [Model Connections](model-connections.md).

## Download A Reviewed Artifact

List the small reviewed catalog:

```bash
heartwood models artifacts
```

Download one artifact to persistent storage:

```bash
heartwood models download qwen25-7b-instruct-q4_k_m --cache /path/to/persistent/models
```

Heartwood downloads from the pinned Hugging Face repository revision and verifies the path, exact byte size, and SHA-256 digest before returning the file. The command prints the final path. Review the model card, license, resource envelope, and deployment policy before use; catalog inclusion is not a production or biomedical-quality endorsement.

Start the included CPU runtime:

```bash
HEARTWOOD_LOCAL_MODEL_PATH=/path/to/persistent/models/qwen25-7b-instruct-q4_k_m/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
  bash images/generic/scripts/start_local_runtime.sh
```

In another shell, refresh the Local connection and select the identifier reported by the server. The local server binds to `127.0.0.1:8765` and does not require external network access after the artifact is present. The artifact catalog also contains `qwen25-coder-7b-instruct-q4_k_m` for coding-output experiments, but it is not the default OpenHands tool-use acceptance artifact because coding text quality and reliable structured tool calling are separate capabilities.

## Verify Mounted Model Tool Execution

The capable-model acceptance script requires a real OpenAI-compatible completion from the mounted artifact, a native OpenHands tool proposal, a successful terminal execution, the exact expected workspace file, allowed model-route records, the selected confirmation mode, no error events, and a scrubbed audit export. It runs with container networking disabled and does not accept a textual description of a tool call as success.

Download the artifact into a named volume while network access is available:

```bash
docker run --rm \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  ghcr.io/schmiedmayerlab/heartwood:edge \
  heartwood models download qwen25-7b-instruct-q4_k_m
```

Then run the complete model and agent path without network access:

```bash
docker run --rm --network none --read-only \
  -v heartwood-models:/models:ro \
  --tmpfs /tmp:rw,nosuid,nodev,size=4g \
  --tmpfs /home/heartwood/.cache:rw,nosuid,nodev,size=1g,uid=10001,gid=10001,mode=0700 \
  --tmpfs /home/heartwood/.openhands:rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 512 \
  -e HEARTWOOD_LOCAL_MODEL_PATH=/models/qwen25-7b-instruct-q4_k_m/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
  ghcr.io/schmiedmayerlab/heartwood:edge \
  bash images/generic/scripts/capable_model_e2e.sh
```

The home-directory tmpfs mounts explicitly use the image's non-root UID and GID. Omitting those ownership options replaces the image-owned directories with root-owned mounts and prevents OpenHands from writing its runtime state.

The reviewed 7B Q4 artifact requires at least 16 GB RAM; 32 GB is recommended. This resource-intensive acceptance run is suitable for local release validation or the opt-in `run_capable_model` workflow-dispatch job, not the default pull-request gate. Pull-request CI keeps the deterministic no-network OpenHands test and a separately mounted tiny llama.cpp inference test so it remains reproducible and does not download multi-gigabyte weights.

## Run The Container UI

Create persistent volumes and start the no-weight interface:

```bash
docker run --rm -p 127.0.0.1:8767:8767 \
  -v heartwood-state:/home/heartwood/.local/share/heartwood \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  ghcr.io/schmiedmayerlab/heartwood:edge \
  bash images/generic/scripts/start_demo_stack.sh
```

Open `http://127.0.0.1:8767/`. Model setup groups models installed in the active local runtime, platform-provided research services, OpenAI, Anthropic, and Custom API. It discovers the source catalog, materializes the selected model as the gateway-owned execution profile, and displays route validation. Deployment-specific execution fields remain under **More options**. Reviewed artifact downloads go to the mounted cache and report transferred bytes until integrity verification completes. An operator must still start the corresponding local inference service or supply a reachable provider endpoint.

To start the included local runtime with the web UI in one container, first download the artifact into the mounted volume, then run:

```bash
docker run --rm -p 127.0.0.1:8767:8767 \
  -v heartwood-state:/home/heartwood/.local/share/heartwood \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  -e HEARTWOOD_DEMO_START_LOCAL_RUNTIME=1 \
  -e HEARTWOOD_LOCAL_MODEL_PATH=/home/heartwood/.cache/heartwood/models/qwen25-7b-instruct-q4_k_m/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
  ghcr.io/schmiedmayerlab/heartwood:edge \
  bash images/generic/scripts/start_demo_stack.sh
```

Choose the model under **On this device** in the UI. CLI and web operations use the same connection catalog, model settings, command/event contract, OpenHands backend, action confirmation, and audit store.

## Work Air-Gapped

Prepare all assets before entering the isolated environment:

1. Pull the exact `sha-<git-sha>` image for the target architecture.
2. Download and verify the selected artifact into a transferable or platform-persistent model directory.
3. Record the image digest, model manifest, model digest, runtime profile, and policy profile.
4. Import the image and model directory through the platform-approved path.
5. Start the local service on loopback, refresh the Local connection, and select its reported model.
6. Disable runtime network access at the platform or container layer.
7. Run a synthetic task, inspect any proposed action, allow or reject it, replay the session, and export the scrubbed audit log.

Heartwood route policy is an application-layer record and gate. Network isolation, filesystem isolation, identity, and controlled-data access remain deployment responsibilities.

## Use An Institution-Approved API

The OpenAI and Anthropic connections retrieve the models visible to the configured provider credential. For example:

```bash
export ANTHROPIC_API_KEY="..."
heartwood models refresh anthropic
heartwood models connect anthropic <model-id>
```

Point `HEARTWOOD_POLICY_PROFILE` to a deployment-owned JSON file that authorizes the exact route and non-secret credential reference:

```json
{
  "schema_version": "heartwood.policy-profile.v1",
  "policy_id": "institutional-models",
  "platform_id": "deployment",
  "deny_egress_by_default": true,
  "allowed_model_catalog_endpoints": [
    "https://api.anthropic.com/v1/models"
  ],
  "allowed_model_endpoints": [
    "https://api.anthropic.com/v1/messages"
  ],
  "allowed_capability_tiers": [
    "supervised"
  ],
  "allowed_action_confirmation_modes": [
    "always-confirm"
  ],
  "credential_allowlist": [
    "ANTHROPIC_API_KEY"
  ],
  "aggregate_count_floor": 20
}
```

Catalog discovery and model completion are separately authorized. `credential_allowlist` entries are environment-variable names, absolute mounted-secret paths, or `managed-identity`, never secret values. A model not verified by the pinned OpenHands SDK is labeled experimental and also requires `experimental` in `allowed_capability_tiers`. Before controlled-data use, the institution must verify the business associate agreement or equivalent contract, the covered product and feature, data retention and training settings, regional processing, account identity, logging, and private or controlled network path. Heartwood does not infer those properties from a provider name.

## Action Confirmation

Heartwood exposes two OpenHands-native modes through `heartwood actions` and the web settings panel:

- **Ask Every Time** is the default and maps to OpenHands `AlwaysConfirm`.
- **Auto-Approve Low Risk** maps to OpenHands `ConfirmRisky` with a `MEDIUM` threshold and unknown actions confirmed.

Both modes use the OpenHands ensemble of deterministic policy-rail and pattern analyzers plus its model risk analyzer. In the automatic mode, only low-risk actions execute without a prompt; medium-, high-, and unknown-risk actions show the pending action identifier and offer two decisions:

- Allow once: execute the current action and continue until the next confirmation or completion.
- Reject: return the rejection to OpenHands and stop without another model call; resume explicitly when further model work is wanted.

Generic synthetic development permits both modes:

```bash
heartwood actions
heartwood actions set auto-approve-low-risk
heartwood actions set ask-every-time
```

Managed policies default to `allowed_action_confirmation_modes: ["always-confirm"]`. Risk analysis is defense in depth rather than a sandbox or complete prompt-injection defense, and OpenHands `NeverConfirm` is not available through researcher settings.

Model route authorization is not a conversational approval. Repository-verified bundled Skills do not prompt on every activation. Community or experimental Skills require a separate installation-time trust decision. Export authorization remains an independent policy decision.

## Install A Skill Extension

Verified bundled Skills are available to OpenHands automatically. A mounted community or experimental extension enters the runtime only through an installation-time trust decision:

```bash
heartwood skills inspect /path/to/mounted-skill
heartwood skills install /path/to/mounted-skill --approve
heartwood skills list
```

Inspection displays the trust tier, declared tools, network requirement, and plain-language permission summary. Installation rejects unsupported tools, network-requiring Skills, path escapes, symbolic links, malformed metadata, and replacement of a bundled Skill. The approved source is copied atomically into persistent Heartwood state and the decision is appended to `skill-installations.jsonl` without prompt or data content. Remove only installed extensions with `heartwood skills remove <name>`.
