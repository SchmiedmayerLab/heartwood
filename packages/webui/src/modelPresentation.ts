/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type { ModelProfile, ModelSettings } from "./types";

export const modelProfileLabel = (
  profile: ModelProfile,
  settings: ModelSettings,
): string => {
  const connection = settings.connections.find(
    (item) => item.connection_id === profile.profile_id,
  );
  if (connection) {
    const modelName =
      profile.model.startsWith(connection.model_prefix) ?
        profile.model.slice(connection.model_prefix.length)
      : profile.model;
    if (connection.connection_id === "heartwood") {
      const description = profile.description?.trim();
      const descriptionPrefix = `${connection.label}: `;
      const describedModel =
        description?.startsWith(descriptionPrefix) ?
          description.slice(descriptionPrefix.length)
        : description;
      const displayName =
        !describedModel || describedModel === modelName ?
          "Managed model"
        : describedModel;
      return `${connection.label} · ${displayName}`;
    }
    return `${connection.label} · ${modelName}`;
  }
  const preset = settings.presets.find(
    (item) => item.preset_id === profile.profile_id,
  );
  if (!preset) return `${profile.profile_id} · ${profile.model}`;
  const modelName =
    profile.model.startsWith(preset.model_prefix) ?
      profile.model.slice(preset.model_prefix.length)
    : profile.model;
  return `${preset.label} · ${modelName}`;
};
