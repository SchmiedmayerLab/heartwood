<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# System Architecture

The session gateway is the application boundary shared by every interaction surface.
It is the only path from Heartwood interfaces to the OpenHands SDK backend.

```mermaid
flowchart LR
    CLI["Terminal interface"] --> Gateway["Session gateway"]
    Web["Browser interface"] --> Ingress["Validated gateway ingress"]
    Ingress --> Gateway
    Notebook["Notebook bridge"] --> Gateway
    Gateway --> Project["Project and .heartwood state"]
    Gateway --> Policy["Platform, model, and action policy"]
    Gateway --> Skills["Verified and project Skills"]
    Gateway --> Adapter["OpenHands SDK adapter"]
    Adapter --> Model["Research-environment, hosted, compatible-service, or Heartwood-managed model"]
    Adapter --> Tools["OpenHands coding tools"]
    Gateway --> Audit["Session events and audit chain"]
```

## Shared Contracts

### Project Context

`ProjectContext.current()` resolves the process current directory and reserves `.heartwood/` beneath it.
No interface accepts a separate public workspace path.

### Startup Plan

The startup planner combines read-only readiness with typed platform capabilities and returns one phase: project review, connection required, credential required, model required, compute required, ready, or recovery required.
Terminal, browser, and notebook clients consume the same projection.

### Session Commands and Events

Interfaces submit typed commands to the gateway.
OpenHands work runs in a supervised background thread so an interface can send guidance, pause, resume, or review an action set while the task is active.
Final messages, actions, lifecycle changes, task plans, usage snapshots, specialist lineage, and errors become typed durable events.
Incremental token text is transient and is never appended to the session or audit log.

### Interface Projections

The gateway reduces durable events and current transient text into one session projection.
That projection contains the conversation, versioned correlated action records, lifecycle, complete pending action set, task plan, total and per-purpose model usage, specialist lineage, activity, available commands, researcher-facing status, and a bounded set of contextual next-step suggestions.
The gateway enforces those available commands before dispatch, so terminal, browser, and notebook clients share the same lifecycle rules.
REST and streaming transports return events and that projection from one serialized snapshot; a transient revision orders token-only updates between durable events.
The terminal, browser, and notebook bridge render the projection without maintaining their own event reducers.

The gateway also owns researcher-facing setup choices, model-connection categories, readiness diagnostics, lifecycle and task labels, usage-purpose labels, specialist summaries, contextual suggestions, and action settings.
Interfaces may present these differently, but they do not infer separate labels, capabilities, or persistence behavior.
Technical identifiers remain in the typed projection for diagnosis and correlation, while presentation adapters keep them out of the primary workflow.

### Workspace Inspection

The gateway owns one bounded read-only workspace service for project trees, UTF-8 text files, changed paths, and per-file diffs.
It accepts normalized project-relative paths, excludes `.heartwood/` and `.git/` at every depth, does not follow symbolic links, and rejects special files.
Every operation applies fixed count, depth, line, and byte bounds.
Tree and changed-path responses publish the active limits, and all responses report unavailable, binary, truncated, non-Git, or unsupported state when applicable.

For Git projects, the service delegates changed-file and diff inspection to the pinned OpenHands `LocalWorkspace` API.
For non-Git projects, it projects only successful paths attributed to typed OpenHands file-editor actions in the selected session.
Terminal command text is never parsed into file evidence.
The terminal, REST API, browser, and notebook bridge adapt this service without maintaining separate workspace roots or change stores.

### Gateway Ingress

`IngressPolicy` is the transport boundary for HTTP and WebSocket requests.
It models direct loopback, a local Jupyter proxy, and an explicitly trusted proxy with one canonical external origin and base path.
It rejects undeclared non-loopback exposure, untrusted forwarding metadata, origin and host mismatches, duplicate security headers, and ambiguous paths before the REST, streaming, or static-asset adapters see a route.

The browser reads its base path from server-injected non-secret metadata.
It does not derive platform proxy paths or reduce gateway events itself.
The selected platform adapter advertises supported ingress modes, while deployment configuration supplies exact proxy values.

### OpenHands Adapter

The adapter creates an OpenHands conversation with `OpenHandsAgentSettings`, the selected LiteLLM-compatible model profile, project workspace, Skills, persistence directory, and confirmation policy.
It uses public typed OpenHands events and conversation state to derive lifecycle, unmatched actions, task progress, usage, and errors.
OpenHands' privacy-safe failure classifications are translated into stable Heartwood diagnostics, and raw conversation-error detail is minimized at the OpenHands file-store boundary before persistence.
OpenHands owns the agent loop, conversation persistence, coding tools, Task Tracker, and sequential specialist execution.
Heartwood translates that state into its stable event contract instead of maintaining a parallel agent loop or pending-action cache.
Persisted non-token progress is reconciled while a run is active, while raw token deltas remain transient.
Standard provider routes use OpenHands' LiteLLM-backed LLM interface.
ChatGPT account access uses OpenHands' native subscription registry, OAuth credential store and refresh, and Codex Responses API transport without a Heartwood token implementation.

The default tool contract enables the OpenHands terminal, project file editor, Task Tracker, and sequential Task tool.
Tool concurrency is one, model switching and Model Context Protocol servers are disabled, and critic refinement is disabled unless a future reviewed contract enables them.

### Research Specialist Catalog

The gateway loads the maintained specialist catalog through OpenHands `AgentDefinition` and registers enabled roles with OpenHands' public agent factory and Task tool.
Heartwood validates presentation metadata, model inheritance, confirmation mode, iteration and usage limits, tool capability, and repository-verified Skill references before registration.
It injects verified OpenHands `Skill` objects directly and disables user, public, and project Skill discovery for child agents.

The enabled planning and review roles are advisory and tool-free.
They inherit the parent's model route, run sequentially through OpenHands, and receive only the evidence delegated by the parent agent.
Heartwood projects OpenHands Task lifecycle, lineage, result, failure, and combined usage into the same gateway-owned session view used by every interface.
It does not add a scheduler, child-agent loop, conversation store, or role-specific interface reducer.

The catalog includes a project-action implementation role but does not register it.
The pinned public OpenHands task interface does not provide the restart-safe child-action approval and cancellation contract Heartwood requires, so a tool-capable child fails closed instead of receiving project tools.

The gateway exposes only non-secret subscription status and short-lived device-code sign-in values to interfaces.
The terminal delegates the complete interactive login to OpenHands; the browser uses OpenHands' supported device-code sign-in methods and retains the opaque pending handle only in gateway memory.

### Model Credential Boundary

The gateway classifies the active model route against the selected platform capability.
A credential-free model requires no model secret.
An application-scrubbed route excludes provider material from supported tool inputs and durable or interface state, but OpenHands model calls and tools still share an operating-system identity.
A platform-isolated route requires a separately authorized model transport plus live synthetic qualification.

The classification is part of the shared model-settings and readiness projections.
The gateway disables Low-Risk Automation for a secret-backed application-scrubbed route and rejects stale persisted combinations before an agent starts.
Terminal, browser, and notebook clients render that decision rather than implementing credential rules.

OpenHands Agent Server and RemoteWorkspace are retained as upstream deployment options, but they place the model client and tools in the same remote agent environment.
They are not treated as model-only isolation without an additional platform boundary.

### Platform Adapter

The selected adapter supplies environment detection, capabilities, ingress modes, model-credential boundary, data-mount declarations, credential allowlists, and default policy.
Generic, Terra, and Carina behavior differs only through these boundaries and startup/runtime orchestration.

## Process Ownership

Project configuration writes use a project-scoped interprocess lock.
Session mutation uses a separate interprocess lease for each session, owned by the gateway process that first handles a command.
The lease remains active until that session service closes.

Command receipts make completed requests idempotent across retries and restarts.
A paired event-and-audit recovery journal completes verified interrupted appends without duplicating either record.
Writer metadata left by a stopped process is treated as stale only after the operating-system lock can be acquired; the new owner then validates recovery state before continuing.

The browser, terminal, and notebook interfaces therefore cannot fork one session by writing from separate processes.
They may continue the session sequentially after the current owner stops, or use distinct session identifiers concurrently.
