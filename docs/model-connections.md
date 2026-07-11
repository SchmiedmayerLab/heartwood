<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Model Connections

Heartwood presents model sources as connections and keeps the selected OpenHands execution details in a non-secret model profile. The web UI and CLI use the same gateway catalog, policy checks, and profile store. Heartwood delegates model requests and provider compatibility to OpenHands and LiteLLM; it does not maintain cloud model identifiers or provider request formats.

## Built-In Connections

| Connection | Catalog source | Credential |
|---|---|---|
| Local | `http://127.0.0.1:8765/v1/models` | None; loopback only |
| OpenAI | Official OpenAI model-list operation | `OPENAI_API_KEY` |
| Anthropic | Official Anthropic model-list operation | `ANTHROPIC_API_KEY` |
| Custom API | OpenAI-compatible `/models` route at a user-supplied HTTPS or loopback base URL | `HEARTWOOD_CUSTOM_MODEL_API_KEY` or a transient web token; optional on loopback |

The Local connection works with Ollama, vLLM, SGLang, llama.cpp, or another service that implements the OpenAI-compatible model-list and chat-completions routes on the configured loopback port. The reviewed artifact catalog and runtime launcher are separate: downloading weights does not make a model selectable until an inference service is running and reports it.

Use the same workflow for every connection:

```bash
heartwood models list
heartwood models refresh <connection-id>
heartwood models connect <connection-id> <model-id>
```

`refresh` preserves exact identifiers returned by the source. A model verified by the pinned OpenHands SDK is available, a model known through LiteLLM but unsuitable for the conversation is disabled, and an unknown model remains selectable as experimental. Deployment policy must explicitly allow the resulting capability tier and completion endpoint before an agent turn can run.

## Credentials

Durable credentials remain external runtime inputs referenced by an environment-variable name, absolute mounted-secret path, or `managed-identity`. The CLI has no token argument and resolves those references from the running environment.

The same-origin web UI may submit an OpenAI, Anthropic, or Custom API token to the running gateway. The gateway authorizes the catalog route before retaining the token in process memory, never returns it, and does not write it to model settings, browser storage, logs, session events, or audit exports. The token is lost when the gateway restarts; restart persistence requires an environment variable, mounted secret, or platform-managed identity.

## Platform-Provided Research Services

Set `HEARTWOOD_MODEL_CONNECTIONS` to an absolute path containing a `heartwood.model-connections.v1` manifest. A platform connection appears under **Research environment** in the web UI and through the same CLI commands as a built-in connection.

A maintained OpenAI-compatible research service can expose its complete catalog directly:

```json
{
  "schema_version": "heartwood.model-connections.v1",
  "connections": [
    {
      "connection_id": "research-ai",
      "label": "Research AI Service",
      "protocol": "openai-compatible",
      "model_prefix": "openai/",
      "source": "platform",
      "credential_kind": "environment",
      "api_key_env": "RESEARCH_MODEL_API_KEY",
      "base_url": "https://models.example.org/v1",
      "catalog_endpoint": "https://models.example.org/v1/models",
      "policy_endpoint": "https://models.example.org/v1/chat/completions",
      "description": "Models authorized for this research workspace.",
      "static_models": []
    }
  ]
}
```

When a platform cannot provide a model-list route, it may supply a static catalog. The deployment owner must enumerate every model available to the current identity and update the manifest when that authorization changes:

```json
{
  "schema_version": "heartwood.model-connections.v1",
  "connections": [
    {
      "connection_id": "research-ai",
      "label": "Research AI Service",
      "protocol": "static",
      "model_prefix": "litellm_proxy/",
      "source": "platform",
      "credential_kind": "managed-identity",
      "catalog_endpoint": null,
      "policy_endpoint": "https://models.example.org/v1/chat/completions",
      "description": "Models authorized for this research workspace.",
      "static_models": ["<authorized-model-a>", "<authorized-model-b>"]
    }
  ]
}
```

Manifests contain no credential values. Connection identifiers must be unique across built-in and platform entries. Remote endpoints require HTTPS, a base URL and its endpoints must share one origin, and unauthenticated connections are limited to loopback. Platform entries may also supply the existing non-secret `api_version`, `aws_region_name`, and `aws_profile_name` model-profile options when the selected LiteLLM provider requires them.

## Deployment Policy

Catalog discovery and model completion are separate egress decisions. A deployment policy must authorize the exact routes and the same non-secret credential reference:

```json
{
  "schema_version": "heartwood.policy-profile.v1",
  "policy_id": "research-models",
  "platform_id": "research-platform",
  "deny_egress_by_default": true,
  "allowed_model_catalog_endpoints": [
    "https://models.example.org/v1/models"
  ],
  "allowed_model_endpoints": [
    "https://models.example.org/v1/chat/completions"
  ],
  "allowed_capability_tiers": ["supervised", "experimental"],
  "allowed_action_confirmation_modes": ["always-confirm"],
  "credential_allowlist": ["RESEARCH_MODEL_API_KEY"],
  "aggregate_count_floor": 20
}
```

Platform network controls remain authoritative. A listed connection does not establish a business associate agreement, provider eligibility, regional configuration, retention behavior, identity binding, or approval for controlled data.

## Custom And Advanced Configuration

Custom API asks only for a server URL, an optional token for loopback, and a model selection. If the service does not implement `/models`, the web UI offers a manual identifier only for a catalog-unavailable response; the CLI equivalent is:

```bash
heartwood models connect custom-api <model-id> \
  --base-url https://provider.example.org/v1 \
  --manual
```

`models add`, `models select`, `models remove`, and the web **More options** editor remain the advanced compatibility path for deployment-specific LiteLLM fields. Researcher workflows should prefer connections so provider prefixes, profile identifiers, credential references, and policy endpoints stay outside the common setup path.
