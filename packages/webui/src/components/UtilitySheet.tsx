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
import {
  Building2,
  Check,
  Cloud,
  Download,
  HardDrive,
  RotateCcw,
  Server,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useMemo, useState } from "react";
import type {
  ActionConfirmationMode,
  ActionSettings,
  CredentialKind,
  ModelArtifacts,
  ModelCatalog,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelConnection,
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
  onConnectModel: (request: ModelConnectRequest) => Promise<void>;
  onDiscoverModels: (request: ModelCatalogRequest) => Promise<ModelCatalog>;
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
    onConnectModel,
    onDownload,
    onDiscoverModels,
    onProfileDraft,
    onRefreshSettings,
    onRemoveProfile,
    onSaveProfile,
    onSelectActionMode,
    onSelectProfile,
    onValidateProfile,
  } = props;
  const [settingsView, setSettingsView] = useState<"models" | "approvals">(
    "models",
  );
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
        <SheetTitle>Settings</SheetTitle>
        <SheetDescription>Models and action approvals</SheetDescription>
      </SheetHeader>
      <div aria-label="Settings view" className="settings-tabs" role="tablist">
        <button
          aria-selected={settingsView === "models"}
          role="tab"
          type="button"
          onClick={() => setSettingsView("models")}
        >
          Models
        </button>
        <button
          aria-selected={settingsView === "approvals"}
          role="tab"
          type="button"
          onClick={() => setSettingsView("approvals")}
        >
          Approvals
        </button>
      </div>

      {settingsView === "models" ?
        <>
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
                    {profileLabel(profile, settings)}
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
                  {validation.policy_decision.decision === "allow" ?
                    "Authorized"
                  : "Not authorized"}
                </Badge>
                <span>
                  {credentialStatusLabel(validation.credential_status)}
                </span>
                <small>
                  {validation.policy_decision.decision === "allow" ?
                    "Allowed by this environment"
                  : validation.policy_decision.reason}
                </small>
              </div>
            : null}
          </section>

          <ModelConnectionSetup
            settings={settings}
            onConnect={onConnectModel}
            onDiscover={onDiscoverModels}
          />

          <section className="panel-section artifact-list">
            <h3>Models available to download</h3>
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
                      <ArtifactDownloadStatus
                        alias={artifact.model_alias}
                        download={download}
                      />
                    </div>
                    <Tooltip
                      tooltip={
                        download?.status === "ready" ?
                          `${artifact.model_alias} is ready`
                        : `Download ${artifact.model_alias}`
                      }
                    >
                      <Button
                        aria-label={`Download ${artifact.model_alias}`}
                        disabled={
                          download?.status === "downloading" ||
                          download?.status === "ready"
                        }
                        isPending={download?.status === "downloading"}
                        size="sm"
                        variant="outline"
                        onClick={() => onDownload(artifact.artifact_id)}
                      >
                        {download?.status === "ready" ?
                          <Check size={15} />
                        : <Download size={15} />}
                      </Button>
                    </Tooltip>
                  </div>
                );
              })
            : <p className="panel-empty">No reviewed models available</p>}
          </section>

          <details className="advanced-section">
            <summary>More options</summary>
            <div className="advanced-section-content">
              <div className="profile-list">
                {settings?.profiles.map((profile) => (
                  <div className="profile-row" key={profile.profile_id}>
                    <button
                      type="button"
                      onClick={() => onProfileDraft(profile)}
                    >
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
      : <section className="panel-section">
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
      }
    </>
  );
};

const ModelConnectionSetup = ({
  settings,
  onConnect,
  onDiscover,
}: {
  settings: ModelSettings | null;
  onConnect: (request: ModelConnectRequest) => Promise<void>;
  onDiscover: (request: ModelCatalogRequest) => Promise<ModelCatalog>;
}) => {
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [selectedModel, setSelectedModel] = useState("");
  const [token, setToken] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [manualModel, setManualModel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const connections = settings?.connections ?? [];
  const connection = connections.find(
    (candidate) => candidate.connection_id === connectionId,
  );
  const effectiveConnection = catalog?.connection ?? connection;
  const groups = connectionGroups(connections);

  const choose = (next: ModelConnection) => {
    setConnectionId(next.connection_id);
    setCatalog(null);
    setSelectedModel("");
    setToken("");
    setBaseUrl(next.base_url ?? "");
    setManualModel("");
    setError(null);
  };

  const discover = async () => {
    if (!connection) return;
    setPending(true);
    setError(null);
    setCatalog(null);
    setSelectedModel("");
    try {
      const discovered = await onDiscover({
        connection_id: connection.connection_id,
        ...(token.trim() ? { token: token.trim() } : {}),
        ...(connection.connection_id === "custom-api" ?
          { base_url: baseUrl.trim() }
        : {}),
        refresh: true,
      });
      setCatalog(discovered);
      const first = discovered.models.find(
        (model) => model.availability !== "unsupported",
      );
      setSelectedModel(first?.model_id ?? "");
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setPending(false);
    }
  };

  const activateModel = async (manual: boolean) => {
    if (!connection) return;
    const modelId = manual ? manualModel.trim() : selectedModel;
    if (!modelId) return;
    setPending(true);
    setError(null);
    try {
      await onConnect({
        connection_id: connection.connection_id,
        model_id: modelId,
        ...(connection.connection_id === "custom-api" ?
          { base_url: baseUrl.trim() }
        : {}),
        ...(manual ? { manual: true } : {}),
      });
      setToken("");
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setPending(false);
    }
  };

  return (
    <section className="panel-section model-connections">
      {groups.map((group) =>
        group.connections.length ?
          <div className="connection-group" key={group.label}>
            <h3>{group.label}</h3>
            <div className="connection-list">
              {group.connections.map((item) => (
                <div className="connection-row" key={item.connection_id}>
                  <span className="connection-icon">
                    <ConnectionIcon connection={item} />
                  </span>
                  <div>
                    <strong>{item.label}</strong>
                    <span>{connectionStatus(item)}</span>
                  </div>
                  <Button
                    aria-pressed={item.connection_id === connectionId}
                    size="sm"
                    variant="outline"
                    onClick={() => choose(item)}
                  >
                    {(
                      item.credential_status === "missing" && item.accepts_token
                    ) ?
                      "Connect"
                    : "Choose"}
                  </Button>
                </div>
              ))}
            </div>
          </div>
        : null,
      )}

      {connection && effectiveConnection ?
        <div className="connection-form">
          <div>
            <strong>{connection.label}</strong>
            <span>{connection.description}</span>
          </div>
          {connection.connection_id === "custom-api" ?
            <label>
              Server URL
              <Input
                placeholder="https://provider.example/v1"
                type="url"
                value={baseUrl}
                onChange={(event) => setBaseUrl(event.target.value)}
              />
            </label>
          : null}
          {(
            connection.accepts_token &&
            (connection.credential_status === "missing" ||
              connection.connection_id === "custom-api")
          ) ?
            <label>
              {connection.connection_id === "custom-api" ?
                "Token (optional for local)"
              : "API token"}
              <Input
                autoComplete="off"
                type="password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
              />
            </label>
          : null}
          <Button
            disabled={
              pending ||
              (connection.connection_id === "custom-api" && !baseUrl.trim()) ||
              (connection.accepts_token &&
                connection.credential_status === "missing" &&
                connection.connection_id !== "custom-api" &&
                !token.trim())
            }
            isPending={pending}
            variant="outline"
            onClick={() => void discover()}
          >
            Load models
          </Button>

          {catalog ?
            <>
              <label>
                Model
                <select
                  aria-label={`Models available from ${connection.label}`}
                  value={selectedModel}
                  onChange={(event) => setSelectedModel(event.target.value)}
                >
                  <option disabled value="">
                    Select a model
                  </option>
                  {catalog.models.map((model) => (
                    <option
                      disabled={model.availability === "unsupported"}
                      key={model.model_id}
                      value={model.model_id}
                    >
                      {model.display_name === model.model_id ?
                        model.model_id
                      : `${model.display_name} - ${model.model_id}`}
                    </option>
                  ))}
                </select>
              </label>
              {catalog.models.length ?
                <ModelChoiceStatus
                  catalog={catalog}
                  selectedModel={selectedModel}
                />
              : <p className="panel-empty">No models available</p>}
              <Button
                disabled={pending || !selectedModel}
                isPending={pending}
                onClick={() => void activateModel(false)}
              >
                Use model
              </Button>
            </>
          : null}

          {error ?
            <div className="connection-error" role="alert">
              <span>{error}</span>
              {(
                connection.connection_id === "custom-api" &&
                supportsManualModelFallback(error)
              ) ?
                <>
                  <label>
                    Model identifier
                    <Input
                      value={manualModel}
                      onChange={(event) => setManualModel(event.target.value)}
                    />
                  </label>
                  <Button
                    disabled={pending || !manualModel.trim()}
                    onClick={() => void activateModel(true)}
                  >
                    Use model
                  </Button>
                </>
              : null}
            </div>
          : null}
        </div>
      : null}
    </section>
  );
};

const ModelChoiceStatus = ({
  catalog,
  selectedModel,
}: {
  catalog: ModelCatalog;
  selectedModel: string;
}) => {
  const model = catalog.models.find(
    (entry) => entry.model_id === selectedModel,
  );
  if (!model) return null;
  const status = modelAvailabilityStatus(model.availability);
  return (
    <div className="model-choice-status" role="status">
      <Badge
        variant={model.availability === "available" ? "secondary" : "outline"}
      >
        {status.label}
      </Badge>
      <span>{status.detail}</span>
    </div>
  );
};

const connectionGroups = (connections: ModelConnection[]) => [
  {
    label: "On this device",
    connections: connections.filter(
      (connection) => connection.connection_id === "local",
    ),
  },
  {
    label: "Research environment",
    connections: connections.filter(
      (connection) => connection.source === "platform",
    ),
  },
  {
    label: "Cloud",
    connections: connections.filter(
      (connection) =>
        connection.source !== "platform" &&
        connection.connection_id !== "local",
    ),
  },
];

const ConnectionIcon = ({ connection }: { connection: ModelConnection }) => {
  if (connection.connection_id === "local") return <HardDrive size={16} />;
  if (connection.source === "platform") return <Building2 size={16} />;
  if (connection.connection_id === "custom-api") return <Server size={16} />;
  return <Cloud size={16} />;
};

const connectionStatus = (connection: ModelConnection): string => {
  if (connection.source === "platform") {
    return connection.credential_status === "missing" ?
        "Setup required"
      : "Provided here";
  }
  if (connection.connection_id === "local") return "Local runtime";
  return connection.credential_status === "missing" ?
      "Not connected"
    : "Connected";
};

const ArtifactDownloadStatus = ({
  alias,
  download,
}: {
  alias: string;
  download: ModelArtifacts["downloads"][number] | undefined;
}) => {
  if (!download) return null;
  if (download.status === "ready") {
    return <small role="status">Ready in model storage</small>;
  }
  if (download.status === "error") {
    return <small role="alert">{download.error ?? "Download failed"}</small>;
  }
  const total = download.bytes_total;
  const downloaded = Math.min(download.bytes_downloaded, total);
  const percentage = Math.round((downloaded / total) * 100);
  return (
    <div className="download-progress" role="status">
      <progress
        aria-label={`Download progress for ${alias}`}
        max={total}
        value={downloaded}
      />
      <small>
        {percentage}% · {formatBytes(downloaded)} of {formatBytes(total)}
      </small>
    </div>
  );
};

const profileLabel = (
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
    <label>
      API version
      <Input
        value={draft.api_version ?? ""}
        onChange={(event) =>
          onDraft({ ...draft, api_version: nullIfEmpty(event.target.value) })
        }
      />
    </label>
    <label>
      AWS region
      <Input
        value={draft.aws_region_name ?? ""}
        onChange={(event) =>
          onDraft({
            ...draft,
            aws_region_name: nullIfEmpty(event.target.value),
          })
        }
      />
    </label>
    <label>
      AWS profile
      <Input
        value={draft.aws_profile_name ?? ""}
        onChange={(event) =>
          onDraft({
            ...draft,
            aws_profile_name: nullIfEmpty(event.target.value),
          })
        }
      />
    </label>
    <Button onClick={onSave}>Save profile</Button>
  </div>
);

const nullIfEmpty = (value: string): string | null => value.trim() || null;

const formatBytes = (value: number): string => {
  const gibibytes = value / 1024 ** 3;
  if (gibibytes >= 1) return `${gibibytes.toFixed(1)} GiB`;
  return `${(value / 1024 ** 2).toFixed(0)} MiB`;
};

const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

const credentialStatusLabel = (status: string): string => {
  if (status === "missing") return "Credential required";
  if (status === "available") return "Credential available";
  if (status === "configured") return "Ready";
  return "Credential status unknown";
};

const modelAvailabilityStatus = (
  availability: ModelCatalog["models"][number]["availability"],
): { label: string; detail: string } => {
  if (availability === "available") {
    return {
      label: "Available",
      detail: "Supported by the current agent runtime",
    };
  }
  if (availability === "experimental") {
    return {
      label: "Experimental",
      detail: "Not yet verified with the current agent runtime",
    };
  }
  return {
    label: "Unavailable",
    detail: "This model does not support the required agent workflow",
  };
};

const supportsManualModelFallback = (error: string | null): boolean =>
  error?.includes("catalog is unavailable") === true ||
  error?.includes("catalog request failed") === true;
