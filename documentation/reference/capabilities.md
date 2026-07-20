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
| Interfaces | Terminal, browser, notebook | Terminal, browser, notebook | Terminal |
| Browser route | Direct loopback | Authenticated Jupyter proxy | Unavailable |
| Heartwood-managed runtimes | llama.cpp, vLLM | llama.cpp, vLLM | vLLM |
| Compute model | Current host | Provisioned Terra compute | Slurm allocation |
| Durable storage | Project directory | Dedicated directory below `/home/jupyter` | Approved project storage |
| Credential backends | Process, system keyring, mounted file | Process, mounted file, managed identity | Process, mounted file |
| Model sources | Heartwood-managed, OpenAI, Anthropic, other compatible service | Heartwood-managed, OpenAI, Anthropic, other compatible service | Heartwood-managed, Stanford AI API Gateway |
| Managed model connection | None | None in the baseline adapter | Stanford AI API Gateway |
| Validation evidence | CI | CI and a published synthetic platform run | CI and a published synthetic platform run |

`published synthetic platform run` means that a versioned artifact has been exercised on the named platform without protected data.
It does not mean institutional approval for every project or model route.

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
