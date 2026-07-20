<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Hosted and Managed Models

Heartwood asks a model service for its available models instead of maintaining a duplicate list of provider model identifiers.
The setup flow presents compatible results returned by the service and keeps manual identifiers as an advanced fallback for compatible endpoints that cannot enumerate models.

## OpenAI or Anthropic

1. Start `heartwood` or open browser settings.
2. Choose **OpenAI** or **Anthropic**.
3. Enter the provider token when prompted.
4. Choose a model returned by the provider API.
5. Review the route and action-confirmation setting.

Heartwood validates that the credential binding, route, model profile, and platform policy agree before enabling requests.

## Research-Environment Connections

Managed platforms can expose a connection through their platform adapter and deployment configuration.
Researchers see **Research environment** and the models returned by that connection without needing to understand its internal gateway topology.

Stanford Carina can expose the Stanford AI API Gateway as this managed category.
The institution's current eligibility, data classification, and usage rules remain authoritative; Heartwood does not infer authorization from platform detection alone.

## Other Compatible Services

Choose **Other compatible service** for an explicitly authorized endpoint that implements the OpenAI models and chat-completions API shapes.
Enter the base URL, select or enter a model, and provide a token only when the service requires one.

Custom URLs are available on the workstation/container and Terra adapters when the active deployment policy permits them.
Heartwood records the selected catalog and completion endpoints in the project policy before making requests.
Stanford Carina exposes only its declared Heartwood-managed and research-environment routes.

### Connect to an Existing Model Server

An OpenAI-compatible server that you started separately is an **Other compatible service**, even when it runs in the same compute environment as Heartwood.
Enter its `/v1` base URL and the model identifier it serves.

From a native Heartwood installation, a loopback service normally uses a URL such as `http://127.0.0.1:8000/v1` and requires no token unless the server was configured with one.
From a Heartwood container, `127.0.0.1` refers to the container itself rather than the host.
Docker Desktop users can use `http://host.docker.internal:8000/v1`; Linux Docker Engine users must explicitly provide an equivalent host mapping, such as `--add-host=host.docker.internal:host-gateway`, before using that address.

The built-in **Run with Heartwood** route is different: Heartwood downloads or imports compatible model files and supervises its own server at `127.0.0.1:8765` in the current compute environment.

## Credentials

Heartwood never accepts provider tokens as normal command-line arguments and never writes raw values to project configuration, session events, logs, browser storage, or audit exports.

Credential resolution follows this order:

1. a value entered for the running Heartwood process;
2. an operator-provided environment or mounted-file binding;
3. an explicitly saved operating-system credential on supported workstations; or
4. a platform identity or secret mechanism supplied by the deployment.

When the system credential store is available, setup offers **Remember securely for this project**.
The saved account is scoped to the project and provider binding; `.heartwood/config.toml` retains only non-secret model configuration.

Forget a stored credential from the browser or terminal:

```bash
heartwood models forget openai
heartwood models forget anthropic
```

Tokens for **Other compatible service** remain process-only because changing the service URL changes the trust boundary.

## Command-Line Catalog Operations

```bash
heartwood models refresh openai
heartwood models connect openai MODEL_ID
heartwood models validate
```

These commands are useful for diagnostics and automation.
The guided setup remains the normal path because it collects credentials without exposing them in shell history and explains unavailable choices.
