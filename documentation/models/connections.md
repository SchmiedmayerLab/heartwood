<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Hosted and Managed Models

Heartwood asks each model service for its available models instead of keeping its own list of provider model identifiers.
Setup presents the compatible results; a manual identifier is an advanced fallback for compatible services that cannot list models.

## Sign In With ChatGPT

**Sign in with ChatGPT** uses supported Codex models through a ChatGPT Plus or Pro subscription.
It is separate from the OpenAI API and uses no API key or API credits.

1. Run `heartwood`, or open browser settings where the browser interface is available.
2. Choose **Sign in with ChatGPT**.
3. Review OpenAI's terms and continue.
4. Open the displayed sign-in page and enter the one-time code.
5. Choose a model from the list maintained by the pinned OpenHands SDK.

OpenHands owns the sign-in flow, the supported-model list, credential storage, token refresh and the Codex Responses API transport.
Heartwood stores only non-secret connection and model metadata in the project.
The route is available on workstations, in containers and on Terra.
A successful sign-in does not authorize controlled data; OpenAI's terms and the research environment's rules remain authoritative.

Sign out from browser settings or with:

```bash
heartwood models forget openai-subscription
```

See the [OpenHands subscription authentication guide](https://docs.openhands.dev/sdk/guides/llm-subscriptions) for the upstream behavior.

## Stanford AI API Gateway

Stanford users with an eligible AI API Gateway key can choose **Stanford AI API Gateway** on a workstation, in a container, on Terra or on Carina.

1. Run `heartwood`, or open browser settings where the browser interface is available.
2. Choose **Stanford AI API Gateway**.
3. Enter the gateway key at the hidden prompt.
4. Choose a model the gateway returns for that key.
5. Review the route and the action-review setting.

Heartwood uses OpenHands' model-capability registry to select the gateway's Chat Completions or Responses route.
The gateway is an institution-managed service, but using it does not authorize a dataset or workflow; Stanford's eligibility, data classification, Data Risk Assessment and project approval requirements apply.
See the [Stanford AI API Gateway service page](https://uit.stanford.edu/service/ai-api-gateway), and run a synthetic tool task before using a newly enabled model with research data.

## Platform Connections

A managed deployment can supply its own model connections, which appear with the institution-managed providers in browser settings, `heartwood models list` and `heartwood setup --deployment MANIFEST`.
The deployment provides their credentials and declares their data handling.
The model list shows the models the service returns for the deployment's credential.
For a model the deployment declares, it shows the declared context window and tool support; for other models, the values OpenHands knows; any other value is shown as unknown.
A model the platform withdraws stays selected until you choose another, and its next model call fails with the platform's model error, [`HW-AGENT-011`](../reference/troubleshooting.md#hw-agent-011-model-configuration-is-invalid) for a model-not-found response.
Operators configure these connections in the [deployment manifest](../reference/configuration.md#model-connections).

## OpenAI API or Anthropic API

1. Run `heartwood`, or open browser settings.
2. Choose **OpenAI API** or **Anthropic**.
3. Enter the provider API key when prompted.
4. Choose a model the provider API returns.
5. Review the route and the action-review setting.

Heartwood checks that the credential binding, route, model profile and platform policy agree before enabling requests.
For OpenAI models that support the Responses API, Heartwood follows OpenHands' registry and authorizes `/v1/responses`; other models use Chat Completions.
The request path is chosen automatically and shared by every interface.
Anthropic uses an API key because OpenHands provides no supported Claude subscription sign-in.

## Other Compatible Services

Choose **Other compatible service** for an authorized endpoint that implements the OpenAI-compatible requests the selected model needs.
Enter the base URL, select or enter a model, and provide an API key only if the service requires one.
The route is available on workstations, in containers and on Terra when the active policy permits user-defined routes; Carina does not offer it.
Heartwood records the selected catalog and completion endpoints in the project policy before making requests, and a model that OpenHands identifies as Responses-capable needs `/responses` on the service as well.
Heartwood cannot declare where such a service sends or retains content, so its [data handling](index.md#data-and-compliance-boundary) is undeclared and a deployment policy that restricts data destinations refuses it.

### Connect to an Existing Model Server

A model server you started yourself is an **Other compatible service**, even on the same machine.
Enter its `/v1` base URL and the model identifier it serves.

From a native installation, a loopback server is usually `http://127.0.0.1:8000/v1`, with no key unless the server requires one.
Inside a Heartwood container, `127.0.0.1` is the container itself.
Docker Desktop provides `http://host.docker.internal:8000/v1`; on Linux Docker Engine, add `--add-host=host.docker.internal:host-gateway` to `docker run` first.

**Run with Heartwood** is different: Heartwood downloads or imports the model files and supervises its own runtime over a private socket, as described in [Run a Model With Heartwood](run-with-heartwood.md).

## Credentials

Heartwood does not accept provider API keys as command-line arguments and never writes API keys or subscription tokens to project configuration, session events, logs, browser storage or audit exports.
In a local launch, the model client and the agent's tools share the Heartwood process identity, so the credential is application-scrubbed rather than process-isolated.
With such a credential, every action set waits for you; **Review Changes** requires a credential-free or platform-isolated route, as described in [Review Modes](../use/actions-audit.md#review-modes).

Heartwood resolves an API key in this order:

1. a value entered for the running process;
2. an operator-provided environment or mounted-file binding;
3. a key saved in the operating-system credential store on a workstation; or
4. a platform identity or secret mechanism supplied by the deployment.

Where a system credential store is available, the browser offers **Remember securely for this project** and `heartwood setup` accepts `--remember-credential`.
The saved entry is scoped to the project and provider; the project configuration keeps only non-secret model settings.
Keys for **Other compatible service** stay process-only, because changing the service URL changes the trust boundary.
ChatGPT sign-in credentials stay in OpenHands' user-level store so that the account can be refreshed and reused across projects.

Forget a saved credential from the browser or terminal:

```bash
heartwood models forget openai
heartwood models forget anthropic
heartwood models forget stanford-ai-api-gateway
heartwood models forget openai-subscription
```

An isolated worker never receives the provider credential: it gets an OpenShell placeholder, which the provider attachment outside the worker resolves.

## Command-Line Catalog Operations

```bash
heartwood models refresh openai-subscription
heartwood models refresh openai
heartwood models connect openai MODEL_ID
heartwood models validate
```

`heartwood models refresh` lists each model with its availability, its context window and whether it supports native tool calls.
These commands serve diagnostics and automation.
Guided setup remains the normal path because it collects credentials without exposing them in shell history and explains unavailable choices.
