<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Gateway API

The Heartwood gateway serves one research project and its sessions.
Its HTTP API supports researcher interfaces; it is not a platform scheduler, a sandbox management API or a credential service for workers.
For APIs and permissions requested from Terra, Seven Bridges or another platform, use [Platform API and Access Requirements](../operate/platform-requirements.md).

## Authoritative Definitions

Export the [platform contract bundle](../operate/platform-contract.md#generate-and-review-the-current-artifacts) from the application revision selected for the deployment.
`gateway.openapi.json` is the complete OpenAPI 3.1 route inventory, including request and response schemas, parameters, status codes and authentication.
It uses JSON Schema 2020-12, with request validation schemas and response serialization schemas.
The OpenAPI `info.version` identifies the application package version; the bundle version and artifact hashes in `contract.json` identify the contract revision.
Pin the artifact hashes as well as the version when reviewing an integration.

The `x-sse-event` and `x-websocket` extensions describe event payloads and WebSocket authentication.
They supplement OpenAPI because WebSocket messages are not REST operations.
The exporter writes files to a new directory; the gateway does not serve an OpenAPI discovery endpoint.
Draft platform-message schemas in the same bundle do not add gateway routes or working platform integrations.
Outside-operation adapter registration and proposal creation are trusted Python embedding interfaces, not HTTP endpoints; see [Outside Operation Admission](../operate/platform-contract.md#outside-operation-admission).
See [Contract Artifacts](../operate/platform-contract.md#contract-artifacts) for maturity and compatibility rules.

## Address and Authentication

All paths below are relative to the exact configured public origin and base path.
For example, `/sessions` at `https://research.example.org/heartwood/project-a` is `https://research.example.org/heartwood/project-a/sessions`.
A proxy must apply the configured prefix handling consistently to HTTP, streaming, WebSockets and browser assets.
The [ingress contract](../operate/security.md#gateway-ingress) defines the accepted host, origin, forwarding metadata and proxy source ranges.

The platform authenticates the researcher and authorizes project access before forwarding requests.
Heartwood then validates ingress and requires `X-Heartwood-Capability` on protected HTTP routes.
This is a gateway capability, not a provider API key, a platform access token or a per-user OAuth token.
The gateway records one launching principal and replaces the `actor_id` supplied in session commands.
Run one gateway per researcher and project; sharing a capability does not preserve individual identity.

Two capability-delivery methods are supported:

- The researcher opens the launch link. The browser submits its one-time token to `POST /launch` as `{"token":"…"}` and receives `{"capability":"…"}`. The token is single-use; this route still validates ingress.
- The operator supplies the capability through `--capability-fd`, and an authenticated proxy injects the header only for the owning researcher. The descriptor is consumed and closed before serving requests.

Capability cookies and environment-variable capability delivery are rejected.
Keep launch tokens and capabilities out of URLs sent to servers, logs, shared browser storage and worker access.
For browser storage isolation, use separate origins for mutually untrusted projects; path prefixes alone do not provide it.

## Resource Groups

The table is an orientation to the generated inventory, not a substitute for its operation-specific schemas.
Capabilities and readiness determine whether a route can perform useful work in the selected deployment.
An advertised route does not grant authority to configure a forbidden model, install an unapproved Skill or execute an action.

| Resource | Principal operations | Result |
|---|---|---|
| Project | `GET /project/capabilities`, `/project/readiness`, `/project/startup`; `POST /project/initialize` | Available interfaces, readiness diagnostics, setup and explicit initialization |
| Sessions | `GET` or `POST /sessions`; `POST /sessions/default`; `GET` or `PATCH /sessions/{session_id}` | Session discovery, creation, default selection and title changes |
| Commands | `POST /sessions/{session_id}/commands` | Command events and the current interface projection |
| Events and projection | `GET /sessions/{session_id}/events`, `/events/stream`, `/projection`; WebSocket `/events` | Durable replay and live interface state |
| Workspace | `GET /sessions/{session_id}/workspace/tree`, `/file`, `/changes`, `/diff` | Bounded project inspection; `file` and `diff` require a `path` query parameter |
| Action settings | `GET /settings/actions`; `PUT /settings/actions/confirmation` | Review mode constrained by deployment policy |
| Model settings | `/settings/models` and its catalog, source, profile, connection, artifact, download and transfer operations | Model selection, validation and managed model files |
| Credentials | `GET /settings/credentials`; `DELETE /settings/credentials/{connection_id}` | Credential status or removal of a saved binding; removal does not revoke a provider key |
| Skills and specialists | `/settings/skills` inspection, install, refresh and removal operations; `GET /settings/specialists` | Available research capabilities and reviewed Skill installation |
| Research records | `GET /research/environment`, `/research/workflows`, `/research/experiments`, `/research/experiments/export` | Verification environment, workflow definitions and experiment records |
| Audit | `GET /sessions/{session_id}/audit-export` | Content-minimized audit export; no checkpoint-signing HTTP endpoint |

## Session Commands and Decisions

A command uses `heartwood.session-command.v1` and must name the same session as its route.
The envelope includes `command_id`, `session_id`, `kind`, `created_at` and a JSON-object `payload`.
The generated schema enumerates command kinds; command handlers validate payloads and current-state availability.
Schema validity alone does not establish that a command is executable.

For example, a synthetic chat command body is:

```json
{
  "schema_version": "heartwood.session-command.v1",
  "command_id": "synthetic-chat-1",
  "session_id": "synthetic-study",
  "kind": "chat",
  "created_at": "2026-11-13T12:00:00Z",
  "payload": {"prompt": "Summarize the column names in the synthetic input."}
}
```

Create or select the session first, resolve readiness requirements, then submit to `/sessions/synthetic-study/commands` through the authenticated route.
Choose a unique command identifier for each new intention.
If the response is lost, retain the same complete envelope for a retry: the gateway uses the identifier and command contents to detect duplicates, and rejects conflicting reuse.
Do not generate a new identifier merely to bypass a conflict or unresolved command.
Interrupted execution can require operator recovery; idempotent command admission does not guarantee exactly-once external effects.
Other mutating routes do not inherit this command-envelope retry contract.

An HTTP `200` command response includes `events` and `projection`; it can contain an `error.recorded` event rather than a successful action.
Inspect the returned events, command outcome and lifecycle, then follow the event stream for continuing work.
A completed HTTP exchange is not proof that a tool or platform operation completed.

For approval, display every action and its exact arguments from the current pending set.
Send `kind: "approve"` or `"deny"` with `payload: {"target_type":"action-set","target_id":"…"}`, using the pending set's `groupId` as the target.
The identifier binds the complete ordered set, tools and structured arguments; changed contents need a new decision.
A decision does not freeze referenced file contents, grant platform permissions or replace outside enforcement.
See [Action Review and Audit](../use/actions-audit.md) for the researcher workflow.

### Required Audit Evidence

A managed deployment can require an independently retained checkpoint before dispatching an approved action.
The decision remains durable while publication is unavailable; HTTP `409` can therefore mean that its required evidence is still unresolved, rather than that the researcher must issue a new decision.
Read the session events and projection, restore the operator-owned evidence service, and retry the same complete command envelope when recovery permits it.
Do not change the command identifier, remove evidence configuration or downgrade the policy to bypass the gate.

The `audit.evidence.recorded` event identifies the decision boundary, effective `mode` and `status` (`pending` or `retained`).
A pending record includes `code: "audit.evidence.unavailable"`; a retained record includes the checkpoint digest and covered audit sequence.
The shared projection reports this state in its activity list.
Buffered mode can expose a gap while ordinary work continues; outside-operation submission and outside-result disclosure still require retained evidence.
These observations describe the gateway's evidence checks, not proof that a platform's retention policy or an external operation is independently qualified.
See [Automatic Evidence Boundaries](../operate/audit-checkpoints.md#automatic-evidence-boundaries) for recovery and trust requirements.

## Replay and Streaming

`GET /sessions/{session_id}/events?after=N` returns durable events after the supplied sequence and the current projection.
Omit `after` for an initial replay.
The projection uses its serialized field names, including `revision`; clients should follow the generated response schemas rather than infer casing from request fields.
Transient text can change the projection without adding a durable event.
Keep the last observed durable sequence and avoid duplicating events when reconciling command responses, replay and streaming.

Server-sent events use `/sessions/{session_id}/events/stream?after=N` with `Content-Type: text/event-stream` and event name `heartwood-session-events`.
Each data payload has the same snapshot structure as replay.
Use a client that can send the capability header, or an authenticated proxy that injects it.
The server does not emit SSE event IDs or consume `Last-Event-ID`; reconnect explicitly with the `after` query parameter.
Disable proxy response buffering and support long-lived connections.

WebSockets use `/sessions/{session_id}/events?after=N` and require the exact configured `Origin`.
A trusted proxy can supply the capability header; otherwise send `{"type":"authenticate","capability":"…"}` as the first JSON frame within five seconds.
The authentication frame is limited to 8,192 bytes, and no events are sent before authorization.
The WebSocket sends snapshots only: submit commands over HTTP.
Authorization rejection closes with `1008`; shutdown or project admission conflicts close with `1012`.

## Errors, Limits and Recovery

HTTP request bodies are bounded to 1 MiB of UTF-8.
Errors use `{"error":"…"}` with an optional stable diagnostic `code`; not every error includes a code.
Use codes where supplied and the response status for recovery, rather than parsing human-readable text.
The exact permitted statuses are listed per operation in OpenAPI.

| Status | Meaning | Client response |
|---|---|---|
| `400` | Malformed request, query, JSON, UTF-8 or ingress metadata | Correct the request; do not repeat it unchanged |
| `401` | Missing or invalid gateway capability | Re-establish the authorized capability through the operator or launch flow |
| `403` | Rejected ingress or launch exchange | Check the configured route, identity boundary and token; do not bypass validation |
| `404` | Unknown resource or unavailable ingress path | Check the exact base path and resource identifier |
| `409` | Command conflict, ownership conflict, storage lock or integrity recovery | Read current state and resolve ownership or recovery; do not force another writer |
| `413` | Body exceeds the size limit | Reduce the request; do not disable the limit |
| `422` | Request or configuration validation failure | Correct fields or resolve the reported readiness condition |
| `503` | Gateway shutdown/stoppage or unavailable verification environment | Reconnect to an available gateway; reconcile mutation outcomes before retrying |

Shutdown ends existing streams and rejects new requests.
Reconnect to the replacement gateway with the retained cursor after it is ready.
Gateway capabilities and streams do not authorize native platform operations; platform-side submission, reconciliation, cancellation and billing retain their own contracts.
