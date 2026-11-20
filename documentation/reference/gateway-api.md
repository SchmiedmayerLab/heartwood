<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Gateway API

The Heartwood gateway serves one research project and its sessions.
Its HTTP API serves researcher interfaces; it is not a platform scheduler, a sandbox management API, or a credential service for workers.
For the APIs and permissions Heartwood needs from a platform, see [Platform API and Access Requirements](../operate/platform-requirements.md).

## Authoritative Definitions

Export the [platform contract bundle](../operate/platform-contract.md#generate-and-review-the-current-artifacts) from the application revision the deployment runs:

```bash
uv run python packages/gateway/scripts/export_platform_contract.py /new/output/directory
```

`gateway.openapi.json` is the complete OpenAPI 3.1 inventory of routes, parameters, request and response schemas, status codes, and the `CapabilityHeader` security scheme, using JSON Schema 2020-12.
The `x-sse-event` and `x-websocket` extensions describe the stream payloads and WebSocket authentication, which OpenAPI operations cannot express.
`info.version` is the application package version; the bundle version and artifact digests in `contract.json` identify the contract revision, so pin the digests as well as the version.
The browser's TypeScript API types are generated from the same Python contracts.
The gateway serves no OpenAPI discovery endpoint.

Draft platform-message schemas in the bundle add no gateway routes.
Outside-operation adapter registration and proposal creation are trusted Python embedding interfaces, not HTTP endpoints; see [Outside Operation Admission](../operate/platform-contract.md#outside-operation-admission) and [Contract Artifacts](../operate/platform-contract.md#contract-artifacts).

## Address and Authentication

Paths are relative to the exact configured public origin and base path.
For example, `/sessions` at `https://research.example.org/heartwood/project-a` is `https://research.example.org/heartwood/project-a/sessions`.
A proxy must apply the same prefix handling to HTTP, streams, WebSockets, and browser assets.
The [ingress contract](../operate/security.md#gateway-ingress) defines the accepted host, origin, forwarding metadata, and proxy sources.

The platform authenticates the researcher and authorizes project access before forwarding a request.
Heartwood then validates ingress and requires the `X-Heartwood-Capability` header on every protected route.
That capability is a gateway secret, not a provider API key, platform token, or per-user OAuth token.
The gateway records one launching principal and replaces any `actor_id` supplied in a command, so run one gateway per researcher and project.

The capability arrives in one of two ways:

- The researcher opens the launch link; the page sends its one-time token to `POST /launch` as `{"token":"…"}` and receives `{"capability":"…"}`. The route still validates ingress, and the token works once.
- The operator starts the gateway with [`--capability-fd`](cli.md#gateway-serve), and an authenticated proxy adds the header only for the owning researcher.

With [`identity-proxy` ingress](../operate/security.md#researcher-identity) the gateway verifies the researcher itself and records an actor derived from the verified issuer and subject, `oidc-` followed by a SHA-256 digest.
A browser signs in at `GET /identity/login`, returns through `GET /oauth_callback`, and exchanges the launch token of that sign-in at `POST /launch`.
Other clients send `Authorization: Bearer` with an access token whose audience contains the gateway's client and whose issuer and subject are the bound researcher's.

Capability cookies and environment delivery are rejected.
Keep tokens and capabilities out of URLs, logs, shared browser storage, and worker access.
Browser storage is isolated per origin, not per path, so mutually untrusted projects need separate origins.

## Resource Groups

Readiness and capabilities decide whether a route can do useful work in a deployment; a route never grants authority to configure a forbidden model, install an unapproved Skill, or execute an action.

| Resource | Operations | Result |
|---|---|---|
| Launch | `POST /launch` | One-time token exchange |
| Project | `GET /project/capabilities`, `/project/readiness`, `/project/startup`, `/project/usage-summary`; `POST /project/initialize` | Interfaces, readiness diagnostics, setup phase, content-free usage counts, explicit initialization |
| Planning | `GET /project/planning`; `POST /project/planning/refresh`, `/project/planning/predictions`, `/project/planning/estimates` | The [planning projection](../operate/platform-contract.md#planning-against-platform-budgets), budget refresh, prediction revisions, platform estimates; the POST routes return `404` where no planning service is connected |
| Sessions | `GET`, `POST /sessions`; `POST /sessions/default`; `GET`, `PATCH /sessions/{session_id}` | Discovery, creation, default selection, title changes |
| Commands | `POST /sessions/{session_id}/commands` | Command events and the current projection |
| Events | `GET /sessions/{session_id}/events`, `/events/stream`, `/projection`; WebSocket `/sessions/{session_id}/events` | Durable replay and live interface state |
| Workspace | `GET /sessions/{session_id}/workspace/tree`, `/file`, `/changes`, `/diff` | Bounded project inspection; `file` and `diff` take a `path` query parameter |
| Action review | `GET /settings/actions`; `PUT /settings/actions/review` | Review mode, default, scope, and selection |
| Models | `/settings/models` and its `active`, `source`, `catalog`, `connect`, `repository`, `profiles`, `artifacts`, `downloads`, `imports`, `transfers`, `subscription/device`, and `validation` operations | Model selection, validation, and managed model files |
| Credentials | `GET /settings/credentials`; `DELETE /settings/credentials/{connection_id}` | Credential status, or removal of a saved binding; removal does not revoke a provider key |
| Skills and specialists | `/settings/skills` with `inspect`, `install`, `local/inspect`, `local/install`, `refresh`, and `DELETE /settings/skills/{name}`; `GET /settings/specialists` | Skill review and installation, specialist roles |
| Research services | `GET`, `POST /settings/mcp-servers`; `DELETE /settings/mcp-servers/{server_id}`; `POST .../test`; `PUT .../tools`, `.../enabled` | [Research MCP servers](../operate/platform-contract.md#research-mcp-servers): list, add, test and list tools, select reviewed tools, enable or disable, disconnect |
| Research records | `GET /research/environment`, `/research/workflows`, `/research/experiments`, `/research/experiments/export` | Verification environment, workflow definitions, experiment records |
| Audit | `GET /sessions/{session_id}/audit-export` | Content-minimized audit export; checkpoint signing has no HTTP endpoint |

## Action Review Settings

`PUT /settings/actions/review` takes `{"mode":"review-all"}`, `"review-changes"`, or `"review-outside-effects"`; the body allows no other field.
Both routes return `ActionSettingsResponse`:

| Field | Meaning |
|---|---|
| `review_mode` | Effective mode for the next action set |
| `default_mode` | Mode used when nothing is selected |
| `scope`, `scope_description` | `project` when stored in protected deployment state, `launch` when held for this gateway process only |
| `change_allowed`, `change_blocked_reason` | Whether review can be reduced now; reduction waits until no session is active |
| `modes` | Each mode with `label`, `description`, the `automatic` rules it applies, `allowed`, and `unavailable_reason` |
| `presentation` | Interface labels for action categories |

A selection outside the deployment ceiling, a mode that needs an isolated worker the gateway does not have, or reduced review while the model credential is not isolated returns `422` with the reason.
See [Action Review Settings](configuration.md#action-review-settings).

## Session Commands and Decisions

A command uses `heartwood.session-command.v1` and names the same session as its route.
The envelope has `command_id`, `session_id`, `kind`, `created_at`, and a JSON-object `payload`; `kind` is one of `chat`, `approve`, `deny`, `pause`, `resume`, `replay`, `audit.export`, or `workflow`.
Handlers validate the payload and current state, so a schema-valid command is not necessarily executable.

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

Use a new `command_id` for each new intention.
If a response is lost, resend the same complete envelope: the gateway detects the duplicate from its identifier and contents and rejects conflicting reuse.
Never change the identifier to get past a conflict or an unresolved command.
Idempotent admission does not make outside effects exactly-once, and other mutating routes do not share this retry contract.

A `200` response carries `events` and `projection` and can contain an `error.recorded` event instead of a completed action.
A completed HTTP exchange is not proof that a tool or platform operation completed; read the events and follow the stream.

### Approving an Action Set

Show every action of the pending set with its exact arguments.
Send `kind: "approve"` or `"deny"` with `payload: {"target_type":"action-set","target_id":"…"}`, where the target is the set's `groupId`.
The identifier binds the complete ordered set, its tools, and structured arguments; changed contents need a new decision.
An action whose `details.kind` is `outside-operation` (a request to a deployment service) or `outside-result` (release of that service's result to the worker and its model route) is decided the same way, but only by the researcher and never by a review mode.
Such actions appear only when an embedding launcher registers an outside-service adapter; Heartwood ships no production adapter and `heartwood gateway serve` installs none.
Actions the gateway allows under a review mode record the `automaticRule` that applied.
A decision does not freeze referenced file contents, grant platform permissions, or replace outside enforcement.
See [Actions and Audit History](../use/actions-audit.md).

### Required Audit Evidence

A managed deployment can require an independently retained checkpoint before an approved action runs.
The decision stays durable while publication is unavailable, so `409` can mean that its evidence is unresolved, not that a new decision is needed.
Restore the operator's evidence service, then resend the same envelope; never change the identifier, remove the evidence configuration, or weaken the policy.

`audit.evidence.recorded` names the decision boundary, the `mode`, and `status` (`pending` or `retained`).
A pending record carries `code: "audit.evidence.unavailable"`; a retained record carries the checkpoint digest and covered audit sequence, and the projection lists the state in its activity.
Buffered mode can leave a gap while ordinary work continues; outside-operation submission and result release still require retained evidence.
See [Automatic Evidence Boundaries](../operate/audit-checkpoints.md#automatic-evidence-boundaries).

## Replay and Streaming

`GET /sessions/{session_id}/events?after=N` returns the durable events after sequence `N` and the current projection; omit `after` for a full replay.
Projection fields use their serialized names, such as `groupId` and `revision`; follow the generated schemas.
Transient text can change the projection without a durable event, so track the last durable sequence and deduplicate across command responses, replay, and streams.

`GET /sessions/{session_id}/events/stream?after=N` is a `text/event-stream` with event name `heartwood-session-events` and the same snapshot as replay.
It sends no event IDs and ignores `Last-Event-ID`; reconnect with `after`.
Disable proxy response buffering and allow long-lived connections.

The WebSocket at `/sessions/{session_id}/events?after=N` requires the exact configured `Origin`.
Without a proxy-supplied header, send `{"type":"authenticate","capability":"…"}` as the first frame, within five seconds and at most 8,192 bytes; nothing is sent before authorization.
The socket only sends snapshots; submit commands over HTTP.
It closes with `1008` on rejected authorization and `1012` on shutdown or a project admission conflict.

## Errors, Limits and Recovery

Request bodies are limited to 1 MiB of UTF-8.
Errors are `{"error":"…"}` with an optional stable `code`; act on the code and status, not the message text.
OpenAPI lists the statuses each operation can return.

| Status | Meaning | Client response |
|---|---|---|
| `400` | Malformed request, query, JSON, UTF-8, or ingress metadata | Correct the request |
| `401` | Missing or invalid capability or identity | Re-establish the capability through the launch link or operator, or sign in again |
| `403` | Rejected ingress or launch exchange | Check the route, identity boundary, and token |
| `404` | Unknown resource or base path | Check the base path and identifier |
| `409` | Command, ownership, or lock conflict; integrity recovery; unresolved required evidence | Read current state and resolve it; never force another writer |
| `413` | Body exceeds the limit | Reduce the request |
| `422` | Validation failure or unmet readiness condition | Correct the fields or resolve the condition |
| `503` | Gateway stopping or verification environment unavailable | Reconnect to a running gateway; reconcile mutations before retrying |

Shutdown ends open streams and rejects new requests; reconnect to the replacement gateway with the retained cursor.
Gateway capabilities and streams do not authorize platform operations, whose submission, reconciliation, cancellation, and billing follow their own contracts.
