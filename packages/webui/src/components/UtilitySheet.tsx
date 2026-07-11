/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Badge } from "@stanfordspezi/spezi-web-design-system/components/Badge";
import { Button } from "@stanfordspezi/spezi-web-design-system/components/Button";
import { Checkbox } from "@stanfordspezi/spezi-web-design-system/components/Checkbox";
import { Input } from "@stanfordspezi/spezi-web-design-system/components/Input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@stanfordspezi/spezi-web-design-system/components/Sheet";
import { Tooltip } from "@stanfordspezi/spezi-web-design-system/components/Tooltip";
import { Download, RotateCcw, ShieldCheck, Trash2 } from "lucide-react";
import { useMemo } from "react";
import type {
  ActionConfirmationMode,
  ActionSettings,
  CredentialKind,
  ModelArtifacts,
  ModelProfile,
  ModelSettings,
  ModelValidation,
  SessionEvent,
  SkillSettings,
  SkillSummary,
} from "../types";
import { buildViewModel } from "../viewModel";
import type { UtilityPanel } from "./SessionRail";

interface UtilitySheetProps {
  actions: ActionSettings | null;
  artifacts: ModelArtifacts | null;
  events: SessionEvent[];
  panel: UtilityPanel;
  profileDraft: ModelProfile;
  skillApproved: boolean;
  skillCandidate: SkillSummary | null;
  skillSettings: SkillSettings | null;
  skillSource: string;
  settings: ModelSettings | null;
  validation: ModelValidation | null;
  onClose: () => void;
  onDownload: (artifactId: string) => void;
  onExportAudit: () => void;
  onInspectSkill: () => void;
  onInstallSkill: () => void;
  onProfileDraft: (profile: ModelProfile) => void;
  onRefreshActivity: () => void;
  onRefreshSettings: () => void;
  onRestoreFocus: () => void;
  onRemoveProfile: (profileId: string) => void;
  onRemoveSkill: (name: string) => void;
  onSaveProfile: () => void;
  onSelectActionMode: (mode: ActionConfirmationMode) => void;
  onSelectProfile: (profileId: string) => void;
  onSetSkillApproved: (approved: boolean) => void;
  onSetSkillSource: (source: string) => void;
  onValidateProfile: (profileId?: string) => void;
}

export const UtilitySheet = (props: UtilitySheetProps) => (
  <Sheet
    open={props.panel !== null}
    onOpenChange={(open) => !open && props.onClose()}
  >
    <SheetContent
      className="utility-sheet"
      size="lg"
      onCloseAutoFocus={(event) => {
        event.preventDefault();
        props.onRestoreFocus();
      }}
    >
      {props.panel === "activity" ?
        <ActivityContent {...props} />
      : null}
      {props.panel === "skills" ?
        <SkillsContent {...props} />
      : null}
      {props.panel === "settings" ?
        <SettingsContent {...props} />
      : null}
    </SheetContent>
  </Sheet>
);

const ActivityContent = ({
  events,
  onExportAudit,
  onRefreshActivity,
}: UtilitySheetProps) => {
  const viewModel = useMemo(() => buildViewModel(events), [events]);
  return (
    <>
      <SheetHeader>
        <SheetTitle>Activity &amp; audit</SheetTitle>
        <SheetDescription>
          {viewModel.eventCount} persisted session events
        </SheetDescription>
      </SheetHeader>
      <div className="sheet-toolbar">
        <Button size="sm" variant="outline" onClick={onRefreshActivity}>
          <RotateCcw size={15} />
          Refresh
        </Button>
        <Button size="sm" variant="outline" onClick={onExportAudit}>
          <Download size={15} />
          Export audit
        </Button>
      </div>
      <div className="activity-list">
        {viewModel.activity.length === 0 ?
          <p className="panel-empty">No events recorded</p>
        : viewModel.activity.map((item) => (
            <div className="activity-row" key={`${item.sequence}-${item.kind}`}>
              <small>{String(item.sequence).padStart(3, "0")}</small>
              <div>
                <strong>{item.label}</strong>
                {item.detail ?
                  <span>{item.detail}</span>
                : null}
              </div>
            </div>
          ))
        }
      </div>
    </>
  );
};

const SkillsContent = ({
  skillApproved,
  skillCandidate,
  skillSettings,
  skillSource,
  onInspectSkill,
  onInstallSkill,
  onRemoveSkill,
  onSetSkillApproved,
  onSetSkillSource,
}: UtilitySheetProps) => (
  <>
    <SheetHeader>
      <SheetTitle>Skills</SheetTitle>
      <SheetDescription>
        Bundled and installed research workflows
      </SheetDescription>
    </SheetHeader>
    <section className="panel-section skill-list">
      <h3>Available</h3>
      {skillSettings?.skills.length ?
        skillSettings.skills.map((skill) => (
          <div className="skill-row" key={`${skill.source}-${skill.name}`}>
            <div>
              <strong>{skill.name}</strong>
              <span>{skill.description}</span>
              <small>
                {skill.trust_tier === "verified" ?
                  "Repository verified"
                : skill.trust_tier}{" "}
                · {skill.source}
              </small>
              <small>No controlled-data approval claim</small>
            </div>
            {skill.source === "installed" ?
              <Tooltip tooltip={`Remove ${skill.name}`}>
                <Button
                  aria-label={`Remove ${skill.name}`}
                  size="sm"
                  variant="outline"
                  onClick={() => onRemoveSkill(skill.name)}
                >
                  <Trash2 size={15} />
                </Button>
              </Tooltip>
            : null}
          </div>
        ))
      : <p className="panel-empty">No Skills available</p>}
    </section>

    <details className="advanced-section">
      <summary>Install an extension</summary>
      <div className="advanced-section-content">
        <label>
          Mounted source directory
          <Input
            value={skillSource}
            onChange={(event) => onSetSkillSource(event.target.value)}
          />
        </label>
        <Button
          disabled={!skillSource.trim()}
          variant="outline"
          onClick={onInspectSkill}
        >
          Inspect
        </Button>
        {skillCandidate ?
          <div className="skill-review">
            <strong>{skillCandidate.name}</strong>
            <span>{skillCandidate.approval_summary}</span>
            <span>
              Tools:{" "}
              {skillCandidate.declared_tools.join(", ") || "None declared"}
            </span>
            <label className="checkbox-control">
              <Checkbox
                aria-label="Approve this installation"
                checked={skillApproved}
                onCheckedChange={(checked) =>
                  onSetSkillApproved(checked === true)
                }
              />
              Approve this installation
            </label>
            <Button disabled={!skillApproved} onClick={onInstallSkill}>
              Install
            </Button>
          </div>
        : null}
      </div>
    </details>
  </>
);

const SettingsContent = (props: UtilitySheetProps) => {
  const {
    actions,
    artifacts,
    profileDraft,
    settings,
    validation,
    onDownload,
    onProfileDraft,
    onRefreshSettings,
    onRemoveProfile,
    onSaveProfile,
    onSelectActionMode,
    onSelectProfile,
    onValidateProfile,
  } = props;
  const applyPreset = (presetId: string) => {
    const preset = settings?.presets.find(
      (item) => item.preset_id === presetId,
    );
    if (!preset) return;
    onProfileDraft({
      ...profileDraft,
      model: preset.model_prefix,
      base_url: preset.base_url,
      policy_endpoint: preset.policy_endpoint ?? profileDraft.policy_endpoint,
      credential_kind: preset.credential_kind,
      api_key_env: preset.api_key_env,
      api_key_file: null,
      description: preset.description,
    });
  };
  return (
    <>
      <SheetHeader>
        <SheetTitle>Model &amp; policy</SheetTitle>
        <SheetDescription>
          Provider routing and OpenHands action approvals
        </SheetDescription>
      </SheetHeader>
      <div className="sheet-toolbar">
        <Button size="sm" variant="outline" onClick={onRefreshSettings}>
          <RotateCcw size={15} />
          Refresh
        </Button>
      </div>

      <section className="panel-section">
        <h3>Active model</h3>
        <div className="inline-control">
          <select
            aria-label="Active model profile"
            value={settings?.active_profile ?? ""}
            onChange={(event) => onSelectProfile(event.target.value)}
          >
            <option disabled value="">
              Not configured
            </option>
            {settings?.profiles.map((profile) => (
              <option key={profile.profile_id} value={profile.profile_id}>
                {profile.profile_id} · {profile.model}
              </option>
            ))}
          </select>
          <Tooltip tooltip="Validate active profile">
            <Button
              aria-label="Validate active model profile"
              disabled={!settings?.active_profile}
              size="sm"
              variant="outline"
              onClick={() =>
                onValidateProfile(settings?.active_profile ?? undefined)
              }
            >
              <ShieldCheck size={16} />
            </Button>
          </Tooltip>
        </div>
        {validation ?
          <div className="validation-status">
            <Badge
              variant={
                validation.policy_decision.decision === "allow" ?
                  "secondary"
                : "destructiveLight"
              }
            >
              {validation.policy_decision.decision}
            </Badge>
            <span>{validation.credential_status}</span>
            <small>{validation.policy_decision.reason}</small>
          </div>
        : null}
      </section>

      <section className="panel-section">
        <h3>Action approvals</h3>
        <div
          aria-label="Action approval mode"
          className="mode-control"
          role="group"
        >
          {actions?.modes.map((option) => (
            <button
              aria-pressed={actions.confirmation_mode === option.mode}
              disabled={!option.allowed}
              key={option.mode}
              title={
                option.allowed ?
                  option.label
                : `${option.label} is not allowed by platform policy`
              }
              type="button"
              onClick={() => onSelectActionMode(option.mode)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </section>

      <section className="panel-section artifact-list">
        <h3>Reviewed local models</h3>
        {artifacts?.artifacts.length ?
          artifacts.artifacts.map((artifact) => {
            const download = artifacts.downloads.find(
              (item) => item.artifact_id === artifact.artifact_id,
            );
            return (
              <div className="artifact-row" key={artifact.artifact_id}>
                <div>
                  <strong>{artifact.model_alias}</strong>
                  <span>{formatBytes(artifact.artifact_size_bytes)}</span>
                  {download ?
                    <small>
                      {download.path ?? download.error ?? download.status}
                    </small>
                  : null}
                </div>
                <Tooltip tooltip={`Download ${artifact.model_alias}`}>
                  <Button
                    aria-label={`Download ${artifact.model_alias}`}
                    disabled={download?.status === "downloading"}
                    isPending={download?.status === "downloading"}
                    size="sm"
                    variant="outline"
                    onClick={() => onDownload(artifact.artifact_id)}
                  >
                    <Download size={15} />
                  </Button>
                </Tooltip>
              </div>
            );
          })
        : <p className="panel-empty">No reviewed models available</p>}
      </section>

      <details className="advanced-section">
        <summary>Provider profiles</summary>
        <div className="advanced-section-content">
          <div className="profile-list">
            {settings?.profiles.map((profile) => (
              <div className="profile-row" key={profile.profile_id}>
                <button type="button" onClick={() => onProfileDraft(profile)}>
                  <strong>{profile.profile_id}</strong>
                  <span>{profile.model}</span>
                </button>
                <Tooltip tooltip={`Remove ${profile.profile_id}`}>
                  <Button
                    aria-label={`Remove ${profile.profile_id}`}
                    size="sm"
                    variant="outline"
                    onClick={() => onRemoveProfile(profile.profile_id)}
                  >
                    <Trash2 size={15} />
                  </Button>
                </Tooltip>
              </div>
            ))}
          </div>
          <ProfileEditor
            draft={profileDraft}
            settings={settings}
            onApplyPreset={applyPreset}
            onDraft={onProfileDraft}
            onSave={onSaveProfile}
          />
        </div>
      </details>
    </>
  );
};

const ProfileEditor = ({
  draft,
  settings,
  onApplyPreset,
  onDraft,
  onSave,
}: {
  draft: ModelProfile;
  settings: ModelSettings | null;
  onApplyPreset: (presetId: string) => void;
  onDraft: (profile: ModelProfile) => void;
  onSave: () => void;
}) => (
  <div className="profile-editor">
    <h3>Profile</h3>
    <label>
      Preset
      <select
        aria-label="Provider preset"
        defaultValue=""
        onChange={(event) => onApplyPreset(event.target.value)}
      >
        <option value="">Custom</option>
        {settings?.presets.map((preset) => (
          <option key={preset.preset_id} value={preset.preset_id}>
            {preset.label}
          </option>
        ))}
      </select>
    </label>
    <label>
      Profile ID
      <Input
        value={draft.profile_id}
        onChange={(event) =>
          onDraft({ ...draft, profile_id: event.target.value })
        }
      />
    </label>
    <label>
      Model
      <Input
        value={draft.model}
        onChange={(event) => onDraft({ ...draft, model: event.target.value })}
      />
    </label>
    <label>
      Base URL
      <Input
        value={draft.base_url ?? ""}
        onChange={(event) =>
          onDraft({ ...draft, base_url: nullIfEmpty(event.target.value) })
        }
      />
    </label>
    <label>
      Policy endpoint
      <Input
        value={draft.policy_endpoint}
        onChange={(event) =>
          onDraft({ ...draft, policy_endpoint: event.target.value })
        }
      />
    </label>
    <label>
      Credentials
      <select
        aria-label="Credential kind"
        value={draft.credential_kind}
        onChange={(event) =>
          onDraft({
            ...draft,
            credential_kind: event.target.value as CredentialKind,
            api_key_env: null,
            api_key_file: null,
          })
        }
      >
        <option value="none">None (loopback only)</option>
        <option value="environment">Environment variable</option>
        <option value="file">Mounted file</option>
        <option value="managed-identity">Managed identity</option>
      </select>
    </label>
    {draft.credential_kind === "environment" ?
      <label>
        API key environment variable
        <Input
          value={draft.api_key_env ?? ""}
          onChange={(event) =>
            onDraft({ ...draft, api_key_env: nullIfEmpty(event.target.value) })
          }
        />
      </label>
    : null}
    {draft.credential_kind === "file" ?
      <label>
        API key file
        <Input
          value={draft.api_key_file ?? ""}
          onChange={(event) =>
            onDraft({ ...draft, api_key_file: nullIfEmpty(event.target.value) })
          }
        />
      </label>
    : null}
    <Button onClick={onSave}>Save profile</Button>
  </div>
);

const nullIfEmpty = (value: string): string | null => value.trim() || null;

const formatBytes = (value: number): string => {
  const gibibytes = value / 1024 ** 3;
  if (gibibytes >= 1) return `${gibibytes.toFixed(1)} GiB`;
  return `${(value / 1024 ** 2).toFixed(0)} MiB`;
};
