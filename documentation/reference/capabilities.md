<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Platform Capabilities

Heartwood selects one platform adapter from environment evidence and serves its capability manifest at `GET /project/capabilities`.
The terminal, browser, and notebook offer only what that manifest declares.

## Current Capability Matrix

| Capability | Workstation or Container | Terra | Stanford Carina |
|---|---|---|---|
| Platform identifier | `generic` | `terra` | `carina` |
| Interfaces | Terminal, browser, notebook | Terminal, notebook | Terminal |
| Browser route | Direct loopback | Unavailable | Unavailable |
| Gateway ingress modes | Direct loopback (default), Jupyter proxy, trusted proxy | Jupyter proxy (default), direct loopback, trusted proxy | Direct loopback (default), trusted proxy |
| Heartwood-managed runtimes | llama.cpp, vLLM | llama.cpp, vLLM | vLLM |
| Compute model | Current host | Provisioned Terra compute | Slurm allocation |
| Durable storage | Project directory | Dedicated directory below `/home/jupyter` | Approved project storage |
| Credential backends | Process, system keyring, mounted file | Process, mounted file, managed identity | Process, mounted file |
| Model credential boundary | Application-scrubbed | Application-scrubbed | Application-scrubbed |
| Model sources | Heartwood-managed, Stanford AI API Gateway, ChatGPT sign-in, OpenAI API, Anthropic, other compatible service | Heartwood-managed, Stanford AI API Gateway, ChatGPT sign-in, OpenAI API, Anthropic, other compatible service | Heartwood-managed, Stanford AI API Gateway |
| Built-in institution-managed connection | Stanford AI API Gateway | Stanford AI API Gateway | Stanford AI API Gateway |
| Declared validation level | CI | CI | CI |

Ingress modes describe the gateway contract available to platform operators; they do not add a researcher interface beyond the **Interfaces** row.
The validation level is the evidence the adapter declares (`ci` or `ci-and-live-synthetic`); it is not an institutional approval.

Application-scrubbed routes keep provider credentials out of supported tools and persisted or interface state, but do not isolate an in-process OpenHands tool from the model client.
Outside an isolated worker, a credential-backed model route therefore limits every built-in platform to **Review Every Action**; credential-free Heartwood-managed inference also allows **Review Changes**.
An isolated worker is a deployment choice, not a platform capability: it comes from the `[worker]` table of an operator [deployment manifest](configuration.md#managed-worker).
The built-in platform policies for local launches permit `review-all` and `review-changes`.

## Startup Projection

`GET /project/startup?interface=web` returns `platform_id`, `interface`, `interface_supported`, `project_root`, `state_root`, `phase`, `summary`, `next_action`, `access_url` when one can be verified, `requires_compute`, `requires_confirmation`, `readiness`, and the complete `capabilities` manifest.
The request is read-only; `POST /project/initialize` records the browser confirmation and creates private project state.
