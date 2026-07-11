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

Compose disables container networking and uses a read-only root filesystem, dropped capabilities, `no-new-privileges`, and temporary write points. Inside that boundary the smoke test starts the deterministic loopback model fixture, creates and validates a local model profile, runs an OpenHands SDK conversation, loads the repository-verified Skills through OpenHands, records the route decision and conversation events, and exports a scrubbed audit log. This proves the offline integration path without downloading or embedding a model.

## Use An Existing Local Service

Start Ollama, vLLM, SGLang, llama.cpp, or another service that provides an OpenAI-compatible chat-completions route. Then add a profile:

```bash
heartwood models add local \
  --model openai/local-model \
  --base-url http://127.0.0.1:8765/v1 \
  --policy-endpoint http://127.0.0.1:8765/v1/chat/completions \
  --credential-kind none \
  --select
heartwood models validate local
heartwood chat
```

Credential kind `none` is accepted only for loopback HTTP endpoints. For a service on another host, use the credential and deployment-policy controls appropriate to that route.

## Download A Reviewed Artifact

List the small reviewed catalog:

```bash
heartwood models artifacts
```

Download one artifact to persistent storage:

```bash
heartwood models download qwen25-coder-7b-instruct-q4_k_m --cache /path/to/persistent/models
```

Heartwood downloads from the pinned Hugging Face repository revision and verifies the path, exact byte size, and SHA-256 digest before returning the file. The command prints the final path. Review the model card, license, resource envelope, and deployment policy before use; catalog inclusion is not a production or biomedical-quality endorsement.

Start the included CPU runtime:

```bash
HEARTWOOD_LOCAL_MODEL_PATH=/path/to/persistent/models/qwen25-coder-7b-instruct-q4_k_m/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf \
  bash images/generic/scripts/start_local_runtime.sh
```

In another shell, configure the loopback profile shown above. The local server binds to `127.0.0.1:8765` and does not require network access after the artifact is present.

## Run The Container UI

Create persistent volumes and start the no-weight interface:

```bash
docker run --rm -p 127.0.0.1:8767:8767 \
  -v heartwood-state:/home/heartwood/.local/share/heartwood \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  ghcr.io/schmiedmayerlab/heartwood:edge \
  bash images/generic/scripts/start_demo_stack.sh
```

Open `http://127.0.0.1:8767/`. Use the model settings control to add, select, and validate profiles or to start a reviewed artifact download. Downloads go to the mounted cache. The web UI does not hide process ownership: an operator must start the corresponding local inference service or supply a reachable provider endpoint.

To start the included local runtime with the web UI in one container, first download the artifact into the mounted volume, then run:

```bash
docker run --rm -p 127.0.0.1:8767:8767 \
  -v heartwood-state:/home/heartwood/.local/share/heartwood \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  -e HEARTWOOD_DEMO_START_LOCAL_RUNTIME=1 \
  -e HEARTWOOD_LOCAL_MODEL_PATH=/home/heartwood/.cache/heartwood/models/qwen25-coder-7b-instruct-q4_k_m/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf \
  ghcr.io/schmiedmayerlab/heartwood:edge \
  bash images/generic/scripts/start_demo_stack.sh
```

Configure the loopback profile from the UI. CLI and web operations use the same model settings, command/event contract, OpenHands backend, action confirmation, and audit store.

## Work Air-Gapped

Prepare all assets before entering the isolated environment:

1. Pull the exact `sha-<git-sha>` image for the target architecture.
2. Download and verify the selected artifact into a transferable or platform-persistent model directory.
3. Record the image digest, model manifest, model digest, runtime profile, and policy profile.
4. Import the image and model directory through the platform-approved path.
5. Start the local service on loopback and validate the model profile.
6. Disable runtime network access at the platform or container layer.
7. Run a synthetic task, inspect any proposed action, allow or reject it, replay the session, and export the scrubbed audit log.

Heartwood route policy is an application-layer record and gate. Network isolation, filesystem isolation, identity, and controlled-data access remain deployment responsibilities.

## Use An Institution-Approved API

Common provider presets expose only the OpenHands/LiteLLM fields. For example:

```bash
export ANTHROPIC_API_KEY="..."
heartwood models add institutional \
  --model anthropic/<model-name> \
  --policy-endpoint https://api.anthropic.com/v1/messages \
  --credential-kind environment \
  --api-key-env ANTHROPIC_API_KEY \
  --select
heartwood models validate institutional
```

Point `HEARTWOOD_POLICY_PROFILE` to a deployment-owned JSON file that authorizes the exact route and non-secret credential reference:

```json
{
  "schema_version": "heartwood.policy-profile.v1",
  "policy_id": "institutional-models",
  "platform_id": "deployment",
  "deny_egress_by_default": true,
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

`credential_allowlist` entries are environment-variable names, absolute mounted-secret paths, or `managed-identity`, never secret values. Before controlled-data use, the institution must verify the business associate agreement or equivalent contract, the covered product and feature, data retention and training settings, regional processing, account identity, logging, and private or controlled network path. Heartwood does not infer those properties from a provider name.

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
