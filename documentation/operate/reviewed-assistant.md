<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Configure a Reviewed Assistant

A reviewed assistant keeps a person in control of every agent action, sends project content only to approved model routes, and keeps a verifiable record of each decision.
Use it for a first deployment and for sensitive projects.

## Apply the Controls

| Control | Setting | Where It Is Set |
|---|---|---|
| Action review | **Review Every Action** | Project action-review setting; the default for every new project |
| Model routes | Only routes approved for the project's data | Platform policy, enforced by platform egress controls |
| Credentials | Keyring, mounted secret, or managed identity | Deployment secret delivery; never project configuration |
| Audit evidence | Signed checkpoints retained outside the project | [Audit Checkpoints and Retention](audit-checkpoints.md) |
| Browser access | Loopback, or one authenticated platform proxy | [Gateway ingress](security.md#gateway-ingress) options |
| Release | A pinned release and verified digest | [Standard research environment](standard-environment.md) record |

The Terra, Carina, and generic platform policies permit both action-review modes and the built-in hosted routes.
A deployment that must forbid **Low-Risk Automation** or a hosted route needs a platform policy that omits it; see [Add a Platform](platform-integration.md#define-policy-and-connections).
Enforce the permitted model routes with platform egress controls as well, because Heartwood policy is defense in depth.

## Know Where Project Content Goes

Heartwood sends prompts, selected project content, and tool results to the active model route.
It keeps session history in the project's private `.heartwood/` state until the project owner deletes it.

| Route | Where Content Goes | Who Governs Retention |
|---|---|---|
| Heartwood-managed model | The inference server on the same compute | The deployment |
| Stanford AI API Gateway | Stanford's institution-managed gateway and its providers | Stanford's agreements for the gateway |
| OpenAI API or Anthropic | The provider's API | The provider's API terms and the account's data controls |
| Sign in with ChatGPT | OpenAI through the subscription account | The subscription's terms |
| Other compatible service | The configured service | The service operator |

Only a Heartwood-managed model keeps model traffic inside the compute environment.
For every other route, confirm the retention terms for the exact account, endpoint, and data class before use.
See [Choose Where Models Run](../models/index.md).

## Know the Limits

A reviewed assistant is not an isolated execution environment.
Approved commands run with the permissions of the Heartwood process and can reach any network destination the platform allows.
Terminal commands can also change project files and `.heartwood/` state, so platform controls must enforce the rules that matter to the deployment.

When a project needs isolated execution, read-only inputs, or quarantined outputs, run Heartwood inside a platform sandbox that provides them.
See [Security and Controlled Data](security.md).

## Verify the Configuration

On a synthetic project:

1. Run `heartwood doctor` and confirm the model route, credential boundary, and policy it reports.
2. Confirm that **Review Every Action** is selected, then allow one action set and reject another.
3. Confirm that a request to a route outside the deployment's approved list fails at the network boundary.
4. Run `heartwood audit verify`, then create and verify a checkpoint with the deployment signer.

The configuration is complete when every action waited for a decision, both decisions appear in the session history, and the checkpoint verifies against the deployment's public key.
