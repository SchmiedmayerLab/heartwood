<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Testing and Evidence

Heartwood separates software correctness, model interoperability, platform behavior, and institutional approval. Public development and continuous integration use synthetic data only.

## Test Layers

1. **Unit tests** cover project paths, detector evidence, model and endpoint normalization, policy, credentials, action settings, artifact verification, Skills, audit chaining, and scrubbing.
2. **Contract tests** require the CLI, browser, notebook, REST, and incremental transports to consume the same gateway commands and events.
3. **OpenHands conformance tests** cover messages, action groups, allow, reject, tool execution, errors, persistence, resume, Skills, and context condensation through public SDK behavior.
4. **Browser system tests** run the built application against a live gateway and deterministic loopback model, execute synthetic Skills, persist outputs, export audit data, and replay the browser session through the CLI.
5. **Container tests** build native architectures, exercise no-weight and offline contracts, load a small mounted llama.cpp artifact, and validate Terra Jupyter and proxy behavior.
6. **Platform orchestration tests** exercise Terra image contracts and Carina installation, scheduler planning, scratch staging, runtime supervision, and cleanup with controlled infrastructure.
7. **Capable-model checks** use an explicitly downloaded model to require a real OpenHands tool proposal and exact synthetic output.

## Evidence Boundaries

- A passing unit or integration test shows the tested software contract.
- A capable-model check shows interoperability for one pinned model, runtime, prompt, and hardware.
- A platform test shows behavior for one immutable artifact in that control plane.
- Institutional approval requires a separate review of data, identity, routes, agreements, operations, and evidence.

No earlier layer implies a later one.

## Required Regression Coverage

Changes to OpenHands integration must cover real typed events, grouped confirmation, rejection, restoration, tools, Skills, and offline container behavior.

Changes to model setup must cover service discovery, route policy, secret handling, public Hugging Face inspection, artifact verification, resource planning, runtime readiness, and interface parity.

Changes to a platform must cover user identity, persistent storage, startup, proxy or terminal access, model routes, action decisions, restart, and scrubbed audit export.

Changes to biomedical Skills must cover deterministic synthetic inputs, malformed and boundary cases, output schemas, and explicit limitations.

## Scientific Claims

Artifact integrity, successful tool execution, and an agent's narrative are not evidence of biomedical validity. Scientific evaluation requires a named dataset, method, benchmark, reviewer, acceptance threshold, and failure analysis outside the ordinary agent session.
