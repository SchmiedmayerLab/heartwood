<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Choose a Model

Heartwood needs a language model to interpret requests and propose coding actions. A connection describes where that model runs and how the current deployment may reach it.

!!! note "Connection is not authorization"

    A successful connection proves technical reachability. The project owner must still verify the endpoint, account, retention settings, agreements, region, and permitted data classification.

## Use Guided Setup

Start `heartwood` from an unconfigured project or open **Settings** in the browser. Guided setup offers:

| Choice | Use when | You provide |
|---|---|---|
| **On this device** | A local service is running or Heartwood should prepare a local model | A listed model or Hugging Face `owner/model` identifier |
| **OpenAI** | The project is authorized for OpenAI | A token, then a model returned by the service |
| **Anthropic** | The project is authorized for Anthropic | A token, then a model returned by the service |
| **Stanford AI API Gateway** | The Stanford-managed route is authorized | A gateway token, then a model returned by the service |

Heartwood discovers available models from the selected service whenever possible instead of maintaining a provider model-name list.

The Stanford option is visible in every build but should be selected only in a Stanford deployment with an authorized key and approved use. Other institutions can supply model connections through deployment configuration.

## Run a Model on This Device

**On this device** combines two paths:

- connect to a running OpenAI-compatible service on the local machine; or
- inspect, download, and launch a supported model with Heartwood.

The interface shows a short release-maintained model list and an **Other Hugging Face model** option. Heartwood attempts to select a compatible artifact and packaged runtime from the repository metadata. Unsupported or ambiguous repositories fail before download and provide the issue-report link.

[Run a Model Locally](getting-started-offline.md) explains formats, resources, downloads, and runtime startup.

## Connect a Hosted Provider

Enter the token only at the private prompt or browser field. Heartwood asks the provider for its current model list, then stores the selected model and a non-secret credential binding.

The token remains in the current process and normally must be entered again after restart. Heartwood does not accept provider tokens as command-line arguments.

## Connect Another Service

The browser exposes **Custom API** under research and advanced connections and accepts its token in the private setup field. The CLI currently reads a remote Custom API token from `HEARTWOOD_CUSTOM_MODEL_API_KEY`; provide it to the same process through an approved deployment secret or shell environment before using the commands below:

```bash
heartwood models refresh custom-api --base-url https://models.example.org/v1
heartwood models connect custom-api <model-id> \
  --base-url https://models.example.org/v1
```

The service must implement compatible model-list and chat-completion routes. Remote endpoints require HTTPS and a token; loopback HTTP is permitted for a service on the same machine and may omit one.

Generic projects can explicitly authorize the selected Custom API origin. Managed platforms require an operator-provided connection and policy; a user-supplied URL does not widen Terra or Carina policy.

## Keep Credentials Out of Project State

Heartwood does not store token values in project configuration, session events, logs, or audit exports.

- Interactive tokens remain only in the terminal or browser-service process.
- A platform may provide an approved mounted secret or managed identity.
- Heartwood removes configured provider-key values from agent terminal subprocess environments.

This environment filtering is not a hard same-user process boundary. Deployments that require tools to be unable to access model credentials need platform-native process isolation.

## Inspect Connections

These commands expose the same model settings for diagnostics and automation:

```bash
heartwood models list
heartwood models refresh <connection-id>
heartwood models connect <connection-id> <model-id>
heartwood models validate
```

Use `heartwood models add` only for operator-reviewed advanced profiles. See [Command Reference](cli-reference.md) for the available model commands.
