<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Platform Capabilities

Heartwood selects one platform adapter from deterministic environment evidence and exposes its capabilities to every interface at `GET /project/capabilities`.
The manifest prevents the terminal, browser, and notebook from advertising routes the platform cannot support.

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

Ingress support describes the gateway contract available to platform operators; it does not make an unavailable researcher interface supported.
Terra and Carina continue to advertise only the interfaces in the **Interfaces** row.

Application-scrubbed routes exclude provider credentials from supported tools and persisted or interface state, but they do not isolate an in-process OpenHands tool from the model client.
Secret-backed model routes therefore require **Review Every Action** on all built-in platforms.
Credential-free Heartwood-managed inference does not place a model credential in Heartwood and may use Low-Risk Automation when platform policy permits it.

## Startup Projection

`GET /project/startup?interface=web` and the equivalent gateway method return:

- selected platform and interface;
- resolved project and state paths;
- supported-interface status;
- current setup phase;
- next action;
- browser access URL when one can be verified;
- compute and confirmation requirements;
- readiness checks; and
- the complete platform capability manifest.

Inspection is read-only.
`POST /project/initialize` records explicit browser confirmation and creates private project state.
