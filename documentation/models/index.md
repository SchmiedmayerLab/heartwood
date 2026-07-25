<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Choose Where Models Run

Heartwood uses a model to reason about the project and OpenHands to manage the coding-agent loop and tools.
The model route determines where project content may be sent, which credentials are needed, and whether Heartwood must start and supervise inference in the current environment.

## Compare the Choices

| Choice in Setup | Best When | You Provide |
|---|---|---|
| **Stanford AI API Gateway** | You have access to Stanford's institution-managed gateway | A Stanford AI API Gateway key, then a model returned by the gateway |
| **Sign in with ChatGPT** | The deployment permits the ChatGPT service and you have a Plus or Pro subscription | Account sign-in, then a model supported by OpenHands |
| **OpenAI API** | The deployment permits the OpenAI API and you have API access | An API key, then a model returned by the API |
| **Anthropic** | The deployment permits Anthropic and you have API access | An API key, then a model returned by the API |
| **Other compatible service** | An authorized service implements the OpenAI API format | The service URL, model, and optional token |
| **Run with Heartwood** | Heartwood should download or import model files and supervise inference on the compute where it is running | A recommended model or Hugging Face model identifier |

Heartwood shows **Stanford AI API Gateway** on supported workstation, container, Terra, and Carina installations.
It shows **Other compatible service** only when the platform permits user-defined routes.
Selecting that option records the exact service endpoints in the project policy; it does not establish that the service may receive the intended data.

## Start With the Simplest Authorized Route

Use the Stanford AI API Gateway when you have an eligible key and the route is approved for the project.
For non-sensitive evaluation, **Sign in with ChatGPT** is the shortest account-based route when a supported subscription is available.
Otherwise, a permitted hosted connection is usually the fastest first experience because it avoids model downloads and hardware planning.

Choose a [Heartwood-managed model](choose-managed.md) when the data-use boundary requires inference to remain in the Heartwood environment, suitable hardware is available, or you need an offline workflow.

## Configure the Selection

Run `heartwood` and follow setup, or reopen **Settings → Models** in the browser.
Both interfaces use the same gateway catalog and write the same non-secret selection to `.heartwood/config.toml`.

List the resulting configuration without exposing credentials:

```bash
heartwood models list
heartwood doctor
```

## Data and Compliance Boundary

Provider availability in the interface means the route is implemented and permitted by the active Heartwood policy.
It does not establish that the provider, account, agreement, or deployment may receive a particular dataset.

Confirm institutional approval and data-use terms before sending controlled content to any model route.
