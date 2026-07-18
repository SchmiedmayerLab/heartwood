<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Deployment Architecture

Heartwood supports three deployment shapes: a generic container or native process, a Terra-derived Jupyter image, and a native Stanford Carina installation.

## Shared Application

Every artifact carries the same Python application, gateway contracts, OpenHands adapter, model settings, Skills, policy layer, session persistence, and audit implementation. Container and Terra artifacts also carry the built browser application; the published native archive does not. Platform integrations may change the base image, installation method, scheduler handoff, persistent path checks, model routes, or proxy path. They must not create another agent or state model.

## Generic Deployment

The generic image is the portable workstation and server artifact. It uses `/workspace` as its default working directory and supports AMD64 and ARM64. The portable image includes CPU llama.cpp; the AMD64 NVIDIA variant adds vLLM.

The generic native installer provides the terminal and Heartwood's Python notebook API but no Jupyter kernel, inference server, or built browser application. Notebook use requires an operator-configured Jupyter environment that exposes the installed API. The installation connects to hosted or existing services unless the operator installs a compatible local server.

## Terra

The Terra image extends a Terra Jupyter base and preserves its user, home, notebook service, entrypoint, kernel behavior, persistent disk, and Leonardo routing. Heartwood registers a separate kernel and serves its browser through Jupyter Server Proxy.

The project must be a dedicated directory below Terra's persistent `/home/jupyter` mount. Portable and NVIDIA image variants share the application contract. Terra publication uses the registry manifest format accepted by Leonardo.

## Stanford Carina

The Carina installer places versioned Heartwood and vLLM environments on approved project storage. The project is a separate current directory. Local inference uses an explicit Slurm request, stages the verified model to job-local scratch, supervises vLLM, and releases compute when the interactive process exits.

The Stanford AI API Gateway is the built-in managed alternative to local inference. The terminal is the documented Carina interface.

## Model Placement

Heartwood normalizes every selected model into one non-secret profile and one policy endpoint. The model may run:

- in the Heartwood process's environment through a packaged local server;
- in an existing loopback OpenAI-compatible service;
- behind an institution-managed endpoint; or
- at a hosted provider authorized by deployment policy.

Model discovery and model use are separately authorized. A connection is not a compliance claim.

## Interface Routing

Local browser use binds to loopback. Terra reuses the authenticated Jupyter proxy instead of adding another login or public port. The web application uses relative API paths so the same built assets work under the proxy prefix.

The terminal remains available when browser routing is absent. The notebook bridge calls the same gateway services from the project process and does not introduce a second remote service.

## Platform Adapter Boundary

Platform adapters own:

- environment detection;
- default route and action policy;
- persistent-storage checks;
- scheduler and local-scratch integration;
- proxy URL construction; and
- platform-specific readiness evidence.

The [platform-image contract](../docs/platform-images.md) defines how another integration must preserve the shared application.
