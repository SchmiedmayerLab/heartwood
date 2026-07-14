<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Choose a Model

A model connection tells Heartwood where models are available and how the deployment authorizes that route. The researcher chooses a model returned by the service; Heartwood does not maintain a handwritten list of current OpenAI, Anthropic, or research-platform model identifiers.

## Choose the Simplest Connection

The web interface groups connections by the decision a researcher needs to make:

| Choice | What You Provide | What Heartwood Discovers |
|---|---|---|
| Research environment | Usually only a model selection | Models exposed by the platform-managed service or identity |
| Local | Nothing for an active loopback service, or an explicit reviewed download | Models reported by the local service and reviewed artifacts available for download |
| OpenAI | A provider token | Models returned by the official OpenAI model-list operation |
| Anthropic | A provider token | Models returned by the official Anthropic model-list operation |
| Custom API | An OpenAI-compatible base URL, optional token, and model selection | Models returned by the service's `/models` route |

The interface keeps provider prefixes, profile identifiers, policy endpoints, and credential-storage details under advanced controls. A platform may preconfigure a connection so the researcher never sees or supplies a token.

## Configure the Terminal

Run bare `heartwood` for the guided flow. Choose Local, OpenAI, Anthropic, or a platform-provided research service, then select one of the models returned by that service. When a provider token is needed, Heartwood reads it through a hidden prompt and keeps it only for the current process.

```bash
cd /path/to/project
heartwood
```

The advanced model commands use the same gateway operations as the browser. They are useful when a local service is already running or the deployment has already supplied a credential binding:

```bash
heartwood models list
heartwood models refresh <connection-id>
heartwood models connect <connection-id> <model-id>
heartwood models validate
```

`refresh` performs policy authorization before contacting a catalog. `connect` stores the selected non-secret profile in the current project's `.heartwood/config.toml`. `validate` checks credential availability and the exact completion route without printing a secret.

For a loopback service using the standard local endpoint:

```bash
heartwood models refresh local
heartwood models connect local <model-id>
```

Use [Local and Offline Models](getting-started-offline.md) when Heartwood should download and manage the local runtime as well.

## Understand Credentials

Heartwood never stores provider token values in `.heartwood/config.toml`, command arguments, session events, logs, or audit exports.

- A token entered in the terminal or web interface remains only in the running gateway process. Restarting that process requires the token again unless the deployment provides a durable secret binding.
- A managed research environment may use an identity or mounted secret that is already available to the Heartwood process.
- A deployment adapter may resolve an environment-backed secret internally, but researchers do not need to pass state paths or use environment variables as normal Heartwood configuration.
- The CLI deliberately has no token command-line argument because shell history and process listings are inappropriate secret transports.

OpenHands receives the selected credential for model requests. Heartwood removes configured environment-referenced provider keys from OpenHands terminal subprocess environments, but this is not a complete same-user process sandbox. A deployment that must make model credentials inaccessible to agent tools needs a platform boundary or a supported OpenHands remote workspace.

## Use a Research Environment Connection

A platform connection may expose one model or a catalog of models. Heartwood stores its non-secret endpoint, credential reference, provider options, and policy in project configuration so the CLI, web interface, and notebook bridge see the same choice.

Stanford Carina includes a guided Stanford AI API Gateway choice. Start Heartwood and select it, or request that source explicitly:

```bash
heartwood setup --model-source stanford-ai-api-gateway
```

The setup discovers model aliases from the gateway rather than embedding them in Heartwood. The applicable Stanford agreement, service configuration, Data Risk Assessment, project approval, and platform controls determine which data classifications may use a selected route.

Other platform integrations provide validated `heartwood.model-connections.v1` connection records through the gateway deployment adapter. Researchers should not hand-edit `.heartwood/config.toml`; operators validate platform defaults before making them available.

## Use a Custom API

The Custom API path accepts an absolute HTTPS base URL, or loopback HTTP for a service on the same machine. The base URL and policy endpoint must share an origin, and remote services require a token or managed platform credential.

If the service implements `/models`, Heartwood displays the exact returned identifiers. If that route is unavailable, the web interface permits a manual identifier and the CLI provides the equivalent advanced form:

```bash
heartwood models connect custom-api <model-id> \
  --base-url https://models.example.org/v1 \
  --manual
```

Provider-specific request construction and tool-call formatting remain owned by OpenHands and LiteLLM. Heartwood does not implement a parallel provider client.

## Route Policy

Catalog discovery and model completion are separate policy decisions. A deployment policy identifies exact allowed catalog endpoints, exact completion endpoints, capability tiers, action-confirmation modes, and non-secret credential references. Heartwood records allow or deny decisions, while platform network controls remain authoritative for actual egress.

Seeing a provider or model in Heartwood is not a compliance claim. Before controlled data is used, the deploying institution must verify the business associate agreement or equivalent contract, covered service and features, identity, region, retention, training use, logging, and network path.
