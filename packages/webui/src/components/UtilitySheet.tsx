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
import { Progress } from "@stanfordspezi/spezi-web-design-system/components/Progress";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@stanfordspezi/spezi-web-design-system/components/Select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@stanfordspezi/spezi-web-design-system/components/Sheet";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@stanfordspezi/spezi-web-design-system/components/Tabs";
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
import { Fragment, useMemo, useState } from "react";
import { modelProfileLabel } from "../modelPresentation";
import type {
  ActionConfirmationMode,
  ActionSettings,
  CredentialKind,
  CredentialStatus,
  ModelArtifacts,
  ModelCatalog,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelConnection,
  ModelProfile,
  ModelSource,
  ModelSettings,
  ModelValidation,
  ProjectReadiness,
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
  projectReadiness: ProjectReadiness | null;
  skillApproved: boolean;
  skillCandidate: SkillSummary | null;
  skillSettings: SkillSettings | null;
  skillSource: string;
  settings: ModelSettings | null;
  validation: ModelValidation | null;
  onClose: () => void;
  onConnectModel: (request: ModelConnectRequest) => Promise<void>;
  onConfigureModelSource: (sourceId: ModelSource) => Promise<ModelSettings>;
  onDiscoverModels: (request: ModelCatalogRequest) => Promise<ModelCatalog>;
  onDownload: (modelId: string) => void;
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
    projectReadiness,
    settings,
    validation,
    onConnectModel,
    onConfigureModelSource,
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
  const localModels = artifacts ? localModelOptions(artifacts) : [];
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
        <SheetTitle>
          {projectReadiness?.state === "setup-required" ?
            "Set up Heartwood"
          : "Settings"}
        </SheetTitle>
        <SheetDescription>
          {projectReadiness?.state === "setup-required" ?
            "Choose a model for this project"
          : "Project model and action approvals"}
        </SheetDescription>
      </SheetHeader>
      <Tabs
        value={settingsView}
        onValueChange={(value) =>
          setSettingsView(value === "approvals" ? "approvals" : "models")
        }
      >
        <TabsList aria-label="Settings view" className="settings-tabs" grow>
          <TabsTrigger value="models">Models</TabsTrigger>
          <TabsTrigger value="approvals">Approvals</TabsTrigger>
        </TabsList>

        <TabsContent className="settings-tab-content" value="models">
          <div className="sheet-toolbar">
            <Button size="sm" variant="outline" onClick={onRefreshSettings}>
              <RotateCcw size={15} />
              Refresh
            </Button>
          </div>
          <ProjectReadinessSummary readiness={projectReadiness} />
          {settings?.profiles.length ?
            <section className="panel-section">
              <h3>Active model</h3>
              <div className="inline-control">
                <Select
                  value={settings.active_profile ?? undefined}
                  onValueChange={onSelectProfile}
                >
                  <SelectTrigger aria-label="Active model profile">
                    <SelectValue placeholder="Not configured" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {settings.profiles.map((profile) => (
                        <SelectItem
                          key={profile.profile_id}
                          value={profile.profile_id}
                        >
                          {modelProfileLabel(profile, settings)}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
                <Tooltip tooltip="Validate active profile">
                  <Button
                    aria-label="Validate active model profile"
                    disabled={!settings.active_profile}
                    size="sm"
                    variant="outline"
                    onClick={() =>
                      onValidateProfile(settings.active_profile ?? undefined)
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
          : null}

          <section className="panel-section artifact-list">
            <h3>On this device</h3>
            {artifacts && localModels.length ?
              localModels.map((model) => {
                const download = artifacts.downloads.find(
                  (item) => item.model_id === model.modelId,
                );
                return (
                  <div className="artifact-row" key={model.modelId}>
                    <div>
                      <strong>{model.label}</strong>
                      <span>
                        {model.compute} · {formatBytes(model.size)}
                      </span>
                      <small>{model.purpose}</small>
                      <ArtifactDownloadStatus
                        alias={model.label}
                        download={download}
                      />
                    </div>
                    <Tooltip
                      tooltip={
                        download?.status === "ready" ?
                          `${model.label} is ready`
                        : `Download ${model.label}`
                      }
                    >
                      <Button
                        aria-label={`Download ${model.label}`}
                        disabled={
                          download?.status === "downloading" ||
                          download?.status === "ready"
                        }
                        isPending={download?.status === "downloading"}
                        size="sm"
                        variant="outline"
                        onClick={() => onDownload(model.modelId)}
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

          <ModelConnectionSetup
            settings={settings}
            onConnect={onConnectModel}
            onConfigureSource={onConfigureModelSource}
            onDiscover={onDiscoverModels}
          />

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
        </TabsContent>
        <TabsContent className="settings-tab-content" value="approvals">
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
        </TabsContent>
      </Tabs>
    </>
  );
};

const ProjectReadinessSummary = ({
  readiness,
}: {
  readiness: ProjectReadiness | null;
}) => {
  if (!readiness) {
    return (
      <section className="panel-section project-readiness" aria-live="polite">
        <h3>This project</h3>
        <span>Checking setup</span>
      </section>
    );
  }
  const status = readinessStatus(readiness.state);
  const attention = readiness.checks.filter((check) => check.status !== "pass");
  return (
    <section className="panel-section project-readiness" aria-live="polite">
      <div className="project-readiness-heading">
        <div>
          <h3>This project</h3>
          <strong>{projectName(readiness.project_root)}</strong>
        </div>
        <Badge variant={status.variant}>{status.label}</Badge>
      </div>
      {attention.slice(0, 2).map((check) => (
        <span key={check.check_id}>{check.summary}</span>
      ))}
      {readiness.state === "compute-required" ?
        <div className="project-next-step">
          <strong>Next step</strong>
          <span>
            Start the selected model and browser together with{" "}
            <code>heartwood launch --web</code> from this project.
          </span>
        </div>
      : null}
      <details>
        <summary>Project details</summary>
        <div className="project-detail-content">
          <small>Platform: {readiness.platform_id}</small>
          <small>Project: {readiness.project_root}</small>
          <ul className="readiness-checks">
            {readiness.checks.map((check) => (
              <li data-status={check.status} key={check.check_id}>
                {check.summary}
              </li>
            ))}
          </ul>
        </div>
      </details>
    </section>
  );
};

const readinessStatus = (
  state: ProjectReadiness["state"],
): { label: string; variant: "destructiveLight" | "outline" | "secondary" } => {
  if (state === "ready") return { label: "Ready", variant: "secondary" };
  if (state === "setup-required")
    return { label: "Setup needed", variant: "outline" };
  if (state === "compute-required")
    return { label: "Model runtime needed", variant: "outline" };
  return { label: "Needs attention", variant: "destructiveLight" };
};

const projectName = (root: string): string => {
  const parts = root.split(/[\\/]/).filter(Boolean);
  return parts.at(-1) ?? root;
};

interface LocalModelOption {
  compute: string;
  label: string;
  modelId: string;
  purpose: string;
  size: number;
}

const localModelOptions = (catalog: ModelArtifacts): LocalModelOption[] => [
  ...catalog.artifacts.map((artifact) => ({
    compute: localComputeLabel(artifact.runtime_profile),
    label: artifact.model_alias,
    modelId: artifact.artifact_id,
    purpose: artifact.purpose,
    size: artifact.artifact_size_bytes,
  })),
  ...catalog.snapshots.map((snapshot) => ({
    compute: localComputeLabel(snapshot.runtime_profile),
    label: snapshot.model_alias,
    modelId: snapshot.snapshot_id,
    purpose: snapshot.purpose,
    size: snapshot.expected_size_bytes,
  })),
];

const localComputeLabel = (runtimeProfile: string): string =>
  runtimeProfile.startsWith("vllm") ? "Requires an NVIDIA GPU" : "Runs on CPU";

const ModelConnectionSetup = ({
  settings,
  onConnect,
  onConfigureSource,
  onDiscover,
}: {
  settings: ModelSettings | null;
  onConnect: (request: ModelConnectRequest) => Promise<void>;
  onConfigureSource: (sourceId: ModelSource) => Promise<ModelSettings>;
  onDiscover: (request: ModelCatalogRequest) => Promise<ModelCatalog>;
}) => {
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [selectedModel, setSelectedModel] = useState("");
  const [token, setToken] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [manualModel, setManualModel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const connections = settings?.connections ?? [];
  const connection = connections.find(
    (candidate) => candidate.connection_id === connectionId,
  );
  const groups = connectionGroups(connections);
  const unconfiguredSources =
    settings?.source_options.filter(
      (source) =>
        !connections.some(
          (candidate) => candidate.connection_id === source.connection_id,
        ),
    ) ?? [];

  const selectConnection = (next: ModelConnection) => {
    setConnectionId(next.connection_id);
    setCatalog(null);
    setSelectedModel("");
    setToken("");
    setBaseUrl(next.base_url ?? "");
    setManualModel("");
    setError(null);
    setSourceError(null);
  };

  const configureSource = async (
    sourceId: ModelSource,
    connectionId: string,
  ): Promise<ModelConnection> => {
    const updated = await onConfigureSource(sourceId);
    const configured = updated.connections.find(
      (candidate) => candidate.connection_id === connectionId,
    );
    if (!configured) throw new Error("Configured model source is unavailable");
    return configured;
  };

  const prepareSource = async (sourceId: ModelSource, connectionId: string) => {
    setPending(true);
    setSourceError(null);
    try {
      selectConnection(await configureSource(sourceId, connectionId));
    } catch (caught) {
      setSourceError(errorMessage(caught));
    } finally {
      setPending(false);
    }
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
      {unconfiguredSources.length ?
        <div className="connection-group">
          <h3>Research environment</h3>
          <div className="connection-list">
            {unconfiguredSources.map((source) => (
              <div className="connection-row" key={source.source_id}>
                <span className="connection-icon">
                  <Building2 size={16} />
                </span>
                <div>
                  <strong>{source.label}</strong>
                  <span>{source.description}</span>
                </div>
                <Button
                  disabled={pending}
                  isPending={pending}
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    void prepareSource(source.source_id, source.connection_id)
                  }
                >
                  Set up
                </Button>
              </div>
            ))}
          </div>
          {sourceError ?
            <div className="connection-error" role="alert">
              {sourceError}
            </div>
          : null}
        </div>
      : null}
      {groups.map((group) =>
        group.connections.length ?
          <div className="connection-group" key={group.label}>
            <h3>{group.label}</h3>
            <div className="connection-list">
              {group.connections.map((item) => (
                <Fragment key={item.connection_id}>
                  <div
                    className={
                      item.connection_id === connectionId ?
                        "connection-row selected"
                      : "connection-row"
                    }
                  >
                    <span className="connection-icon">
                      <ConnectionIcon connection={item} />
                    </span>
                    <div>
                      <strong>{item.label}</strong>
                      <span>{connectionStatus(item)}</span>
                    </div>
                    <Button
                      aria-expanded={item.connection_id === connectionId}
                      aria-pressed={item.connection_id === connectionId}
                      disabled={pending}
                      size="sm"
                      variant="outline"
                      onClick={() => selectConnection(item)}
                    >
                      {(
                        item.credential_status === "missing" &&
                        item.accepts_token
                      ) ?
                        "Connect"
                      : "Choose"}
                    </Button>
                  </div>
                  {item.connection_id === connectionId && connection ?
                    <ModelConnectionForm
                      baseUrl={baseUrl}
                      catalog={catalog}
                      connection={connection}
                      error={error}
                      manualModel={manualModel}
                      pending={pending}
                      selectedModel={selectedModel}
                      token={token}
                      onActivate={activateModel}
                      onBaseUrl={setBaseUrl}
                      onDiscover={discover}
                      onManualModel={setManualModel}
                      onSelectedModel={setSelectedModel}
                      onToken={setToken}
                    />
                  : null}
                </Fragment>
              ))}
            </div>
          </div>
        : null,
      )}
    </section>
  );
};

interface ModelConnectionFormProps {
  baseUrl: string;
  catalog: ModelCatalog | null;
  connection: ModelConnection;
  error: string | null;
  manualModel: string;
  pending: boolean;
  selectedModel: string;
  token: string;
  onActivate: (manual: boolean) => Promise<void>;
  onBaseUrl: (value: string) => void;
  onDiscover: () => Promise<void>;
  onManualModel: (value: string) => void;
  onSelectedModel: (value: string) => void;
  onToken: (value: string) => void;
}

const ModelConnectionForm = ({
  baseUrl,
  catalog,
  connection,
  error,
  manualModel,
  pending,
  selectedModel,
  token,
  onActivate,
  onBaseUrl,
  onDiscover,
  onManualModel,
  onSelectedModel,
  onToken,
}: ModelConnectionFormProps) => (
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
          onChange={(event) => onBaseUrl(event.target.value)}
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
          onChange={(event) => onToken(event.target.value)}
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
      onClick={() => void onDiscover()}
    >
      Load models
    </Button>

    {catalog ?
      <>
        <label>
          Model
          <Select
            search={{
              placeholder: "Search models",
              emptyMessage: "No matching models",
            }}
            value={selectedModel || undefined}
            onValueChange={onSelectedModel}
          >
            <SelectTrigger
              aria-label={`Models available from ${connection.label}`}
            >
              <SelectValue placeholder="Select a model" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {catalog.models.map((model) => (
                  <SelectItem
                    disabled={model.availability === "unsupported"}
                    itemText={modelOptionLabel(model)}
                    key={model.model_id}
                    value={model.model_id}
                  >
                    {modelOptionLabel(model)}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </label>
        {catalog.models.length ?
          <ModelChoiceStatus catalog={catalog} selectedModel={selectedModel} />
        : <p className="panel-empty">No models available</p>}
        <Button
          disabled={pending || !selectedModel}
          isPending={pending}
          onClick={() => void onActivate(false)}
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
                onChange={(event) => onManualModel(event.target.value)}
              />
            </label>
            <Button
              disabled={pending || !manualModel.trim()}
              onClick={() => void onActivate(true)}
            >
              Use model
            </Button>
          </>
        : null}
      </div>
    : null}
  </div>
);

const modelOptionLabel = (model: ModelCatalog["models"][number]): string => {
  const identity =
    model.display_name === model.model_id ?
      model.model_id
    : `${model.display_name} - ${model.model_id}`;
  if (model.availability === "experimental")
    return `${identity} · Experimental`;
  if (model.availability === "unsupported") return `${identity} · Unavailable`;
  return identity;
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
        !["custom-api", "local"].includes(connection.connection_id),
    ),
  },
  {
    label: "Other model services",
    connections: connections.filter((connection) =>
      ["custom-api", "local"].includes(connection.connection_id),
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
  if (connection.connection_id === "local") return "Already running locally";
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
    return <small role="status">Ready for Heartwood launch</small>;
  }
  if (download.status === "error") {
    return <small role="alert">{download.error ?? "Download failed"}</small>;
  }
  const total = download.bytes_total;
  const downloaded = Math.min(download.bytes_downloaded, total);
  const percentage = Math.round((downloaded / total) * 100);
  return (
    <div className="download-progress" role="status">
      <Progress
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

const credentialStatusLabel = (status: CredentialStatus): string =>
  ({
    available: "Credential available",
    configured: "Ready",
    missing: "Credential required",
  })[status];

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
