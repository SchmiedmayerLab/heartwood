/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type { CommandKind } from "./types";

export interface RequestActivity {
  label: string;
  waitingLabel: string;
  guidance: string;
}

const taskActivity: RequestActivity = {
  label: "Heartwood is working on your task",
  waitingLabel: "Heartwood is still working on your task",
  guidance: "Response time depends on the selected model and task.",
};

const activities: Record<CommandKind, RequestActivity> = {
  chat: taskActivity,
  run: taskActivity,
  approve: {
    label: "Continuing the approved action set",
    waitingLabel: "Still continuing the approved action set",
    guidance: "The model may need time to process the tool results.",
  },
  deny: {
    label: "Rejecting the action set",
    waitingLabel: "Still rejecting the action set",
    guidance: "Heartwood is waiting for the session to settle.",
  },
  pause: {
    label: "Pausing the session",
    waitingLabel: "Still pausing the session",
    guidance: "Heartwood is waiting for the active operation to stop safely.",
  },
  resume: {
    label: "Resuming the session",
    waitingLabel: "Still resuming the session",
    guidance: "Response time depends on the selected model and task.",
  },
  replay: {
    label: "Loading the conversation",
    waitingLabel: "Still loading the conversation",
    guidance: "A long session can take additional time to restore.",
  },
  detect: {
    label: "Inspecting the project environment",
    waitingLabel: "Still inspecting the project environment",
    guidance: "Platform services can take additional time to respond.",
  },
  "audit.export": {
    label: "Preparing the audit export",
    waitingLabel: "Still preparing the audit export",
    guidance: "Large session histories can take additional time to process.",
  },
};

export const requestActivityForCommand = (kind: CommandKind): RequestActivity =>
  activities[kind];
