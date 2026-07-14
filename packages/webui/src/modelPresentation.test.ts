/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { describe, expect, it } from "vitest";
import { modelProfileLabel } from "./modelPresentation";
import type { ModelProfile, ModelSettings } from "./types";

const profile = (profileId: string, model: string): ModelProfile => ({
  profile_id: profileId,
  model,
  policy_endpoint: "https://models.example/v1/chat/completions",
  capability_tier: "supervised",
  base_url: null,
  credential_kind: "managed-identity",
  api_key_env: null,
  api_key_file: null,
  api_version: null,
  aws_region_name: null,
  aws_profile_name: null,
  description: null,
});

const settings: ModelSettings = {
  schema_version: "heartwood.model-settings.v1",
  active_profile: null,
  model_source: null,
  profiles: [],
  connections: [
    {
      connection_id: "research",
      label: "Research Models",
      protocol: "static",
      model_prefix: "litellm_proxy/",
      source: "platform",
      credential_kind: "managed-identity",
      policy_endpoint: "https://models.example/v1/chat/completions",
      catalog_endpoint: null,
      base_url: null,
      api_key_env: null,
      api_key_file: null,
      api_version: null,
      aws_region_name: null,
      aws_profile_name: null,
      description: "",
      static_models: [],
      accepts_token: false,
      credential_status: "configured",
    },
  ],
  presets: [
    {
      preset_id: "openai",
      label: "OpenAI",
      model_prefix: "openai/",
      credential_kind: "environment",
      api_key_env: "OPENAI_API_KEY",
      base_url: null,
      policy_endpoint: "https://api.openai.com/v1/chat/completions",
      description: "",
    },
  ],
  source_options: [],
};

describe("modelProfileLabel", () => {
  it("uses the researcher-facing connection label", () => {
    expect(
      modelProfileLabel(profile("research", "litellm_proxy/coder"), settings),
    ).toBe("Research Models · coder");
  });

  it("uses an advanced preset label when no connection exists", () => {
    expect(modelProfileLabel(profile("openai", "openai/gpt"), settings)).toBe(
      "OpenAI · gpt",
    );
  });

  it("preserves a preset model that does not use the expected prefix", () => {
    expect(modelProfileLabel(profile("openai", "gateway/gpt"), settings)).toBe(
      "OpenAI · gateway/gpt",
    );
  });

  it("preserves unknown profile and model identifiers", () => {
    expect(
      modelProfileLabel(profile("custom", "provider/model"), settings),
    ).toBe("custom · provider/model");
  });
});
