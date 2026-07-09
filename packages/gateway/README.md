<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood-gateway

Session gateway for Heartwood command and event streams.

The package owns ASGI HTTP command handling, replayable ASGI WebSocket event streaming, the managed local agent-server boundary, and the policy-gated model-call path. The default path is deterministic and offline for tests and synthetic replay, and the generic image can enable a gateway-owned localhost OpenHands agent-server child through environment configuration. The `openhands-bash` backend calls authenticated OpenHands `/api` routes to execute the bounded offline smoke tool path behind the same session contract.
