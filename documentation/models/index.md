<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Choose a Model Route

The model reasons about the project; OpenHands runs the agent loop and tools around it.
The model route decides where project content is sent, which credential is needed, and whether Heartwood starts and supervises inference itself.

## Compare the Choices

| Choice in Setup | Use When | You Provide |
|---|---|---|
| **Stanford AI API Gateway** | You have access to Stanford's institution-managed gateway | A gateway key, then a model the gateway returns |
| **Sign in with ChatGPT** | The deployment permits the ChatGPT service and you have a Plus or Pro subscription | Account sign-in, then a model OpenHands supports |
| **OpenAI API** | The deployment permits the OpenAI API | An API key, then a model the API returns |
| **Anthropic** | The deployment permits the Anthropic API | An API key, then a model the API returns |
| **Other compatible service** | An authorized service implements the OpenAI API format | The service URL, a model and an optional token |
| **Run with Heartwood** | Inference should stay on the compute where Heartwood runs | A recommended model or a Hugging Face model identifier |

Setup lists only the routes that the detected platform permits.
Workstations, containers and Terra offer all six; Carina offers **Stanford AI API Gateway** and **Run with Heartwood**.
Selecting **Other compatible service** records the exact service endpoints in the project policy; it does not establish that the service may receive the intended data.

## Start With the Simplest Authorized Route

Use the Stanford AI API Gateway when you have an eligible key and the route is approved for the project.
For non-sensitive evaluation, **Sign in with ChatGPT** is the shortest account-based route.
A permitted hosted connection avoids model downloads and hardware planning, so it is usually the fastest first run.

Choose a [Heartwood-managed model](choose-managed.md) when inference must stay in the Heartwood environment, suitable hardware is available, or you need an [offline workflow](offline.md).

## Configure the Selection

Run `heartwood` and follow setup, or open **Settings → Models** in the browser.
Both write the same non-secret selection to the project configuration.
Inspect it without exposing credentials:

```bash
heartwood models list
heartwood doctor
```

The model route also limits the [review modes](../use/actions-audit.md#review-modes): with an API key, subscription or mounted secret in a local launch, every action set waits for you; a credential-free route such as a Heartwood-managed model allows **Review Changes**.

## Models in an Operator-Bound Deployment

In an operator-bound deployment, the manifest's policy sets which model endpoints are allowed.
An isolated worker uses the model, provider and HTTPS endpoint fixed in the manifest's `[worker]` section, and receives an OpenShell placeholder rather than the provider credential.
Heartwood-managed models serve local launches only; they are not available to an isolated worker.
See [Managed Worker](../reference/configuration.md#managed-worker) for the manifest keys.

## Data and Compliance Boundary

A route shown in setup is implemented and permitted by the active Heartwood policy.
That does not establish that the provider, account, agreement or deployment may receive a particular dataset.

Each connection carries a data-handling descriptor: the declared destination of model requests, including prompts, file contents and tool results, and the authority that governs their retention.
Terminal setup, browser settings and the notebook bridge show it before a model is selected, and `heartwood models list` and `heartwood models validate` show it for the active route.

| Route | Declared Destination | Retention Governed By |
|---|---|---|
| **Run with Heartwood** | `same-compute`: the Heartwood-managed inference server in this compute environment | `deployment` |
| **Stanford AI API Gateway** | `institution-managed`: Stanford's gateway and the model providers it routes to | `institution`: Stanford's agreements for the gateway |
| **OpenAI API** or **Anthropic** | `provider`: the provider's API | `provider`: the API terms and the account's data controls |
| **Sign in with ChatGPT** | `provider`: OpenAI through the subscription account | `provider`: the subscription's terms |
| **Other compatible service** | Not declared | Not declared |

A descriptor states declared handling; it is not verified provider behavior, data eligibility or an agreement.
It describes the complete inference route, including an institution's gateway and its ultimate provider; an outside transport proxy that substitutes credentials does not change the declared recipient.
A service URL entered for **Other compatible service** carries no declaration, because its handling depends on its operator; a deployment that needs one supplies the service as a platform connection with a descriptor.
A descriptor covers model inference requests; Heartwood has no transcription or embedding route, and a telemetry or trace exporter that a deployment enables is a separate destination for its own review.

Every model-route decision in the session history records the descriptor of the route that the next model call uses, so a route change is recorded before its first request.
The scrubbed audit export keeps the destination, retention authority and route description without credentials or content.
A deployment policy can list `allowed_data_destinations`; Heartwood then refuses to select, and denies before each model call, a route whose descriptor is missing or names another destination.
See [Policy Fields](../reference/configuration.md#policy-fields) for the policy field.

Session history is kept in Heartwood's project state as well; see [Projects and Private State](../start/project.md).
Confirm institutional approval and data-use terms before sending controlled content to any route.
