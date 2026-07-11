<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood Gateway

Session gateway for Heartwood command and event streams.

The package owns persisted session creation, listing, titles, derived status, and update timestamps; ASGI command handling; replayable WebSocket and Server-Sent Events streams; non-secret model and action settings; gateway-expanded provider presets; reviewed local-artifact downloads with byte progress; and the OpenHands SDK backend adapter. It authorizes the selected endpoint, capability tier, credential reference, and action-confirmation mode before lazily creating an OpenHands conversation; configures the upstream OpenHands analyzer ensemble and confirmation policy; loads repository-verified Skills through OpenHands; and translates messages, actions, confirmations, and observations into the shared Heartwood event contract. Provider credentials are resolved only after policy allows the reference and are never written to settings or audit records.
