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
  Search,
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
  CustomLocalModelDownloadRequest,
  LocalModelChoice,
  LocalModelImportRequest,
  ModelArtifacts,
  ModelCatalog,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelConnection,
  ModelProfile,
  ModelRepositoryPlan,
  ModelRepositoryRequest,
  ModelSource,
  ModelSettings,
  ModelValidation,
  ProjectReadiness,
  SessionEvent,
  SkillSettings,
  SkillSummary,
  StartupPlan,
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
  startupPlan: StartupPlan | null;
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
  onForgetCredential: (connectionId: string) => Promise<void>;
  onDownload: (modelId: string) => void;
  onDownloadCustom: (request: CustomLocalModelDownloadRequest) => Promise<void>;
  onExportAudit: () => void;
  onInspectSkill: () => void;
  onInspectModelRepository: (
    request: ModelRepositoryRequest,
  ) => Promise<ModelRepositoryPlan>;
  onImportLocalModel: (request: LocalModelImportRequest) => Promise<void>;
  onInitializeProject: () => Promise<void>;
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
    onDownloadCustom,
    onDiscoverModels,
    onForgetCredential,
    onInspectModelRepository,
    onImportLocalModel,
    onInitializeProject,
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
          <ProjectReadinessSummary
            readiness={projectReadiness}
            startup={props.startupPlan}
            onInitialize={onInitializeProject}
          />
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
            <h3>Models Heartwood can run</h3>
            {artifacts && localModels.length ?
              localModels.map((model) => {
                const download = artifacts.downloads.find(
                  (item) => item.model_id === model.model_id,
                );
                return (
                  <div className="artifact-row" key={model.model_id}>
                    <div>
                      <strong>
                        {model.label}
                        {model.catalog_source === "recommended" ?
                          <Badge variant="outline">Recommended</Badge>
                        : null}
                      </strong>
                      <span>
                        {localComputeLabel(model.runtime)} ·{" "}
                        {formatBytes(model.size_bytes)} · Up to{" "}
                        {model.context_window.toLocaleString()} tokens
                      </span>
                      <small>{model.purpose}</small>
                      <small>{model.availability_reason}</small>
                      {model.recommended_resource_envelope ?
                        <small>{model.recommended_resource_envelope}</small>
                      : null}
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
                          !model.available ||
                          download?.status === "downloading" ||
                          download?.status === "ready"
                        }
                        isPending={download?.status === "downloading"}
                        size="sm"
                        variant="outline"
                        onClick={() => onDownload(model.model_id)}
                      >
                        {download?.status === "ready" ?
                          <Check size={15} />
                        : <Download size={15} />}
                      </Button>
                    </Tooltip>
                  </div>
                );
              })
            : <p className="panel-empty">No recommended models available</p>}
          </section>

          <CustomLocalModelSetup
            downloads={artifacts?.downloads ?? []}
            onDownload={onDownloadCustom}
            onImport={onImportLocalModel}
            onInspect={onInspectModelRepository}
          />

          <ModelConnectionSetup
            settings={settings}
            onConnect={onConnectModel}
            onConfigureSource={onConfigureModelSource}
            onDiscover={onDiscoverModels}
            onForgetCredential={onForgetCredential}
          />

          <details className="advanced-section">
            <summary>More options</summary>
            <div className="advanced-section-content">
              <div className="profile-list">
                {settings?.profiles
                  .filter((profile) => profile.profile_id !== "heartwood")
                  .map((profile) => (
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
  startup,
  onInitialize,
}: {
  readiness: ProjectReadiness | null;
  startup: StartupPlan | null;
  onInitialize: () => Promise<void>;
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
      {startup ?
        <span>{startup.summary}</span>
      : null}
      {attention.slice(0, 2).map((check) => (
        <div className="readiness-attention" key={check.check_id}>
          <strong>{check.title ?? "Setup needs attention"}</strong>
          <span>{check.summary}</span>
          {check.code ?
            <small>{check.code}</small>
          : null}
        </div>
      ))}
      {startup && startup.phase !== "ready" ?
        <div className="project-next-step">
          <strong>Next step</strong>
          <span>{startup.next_action}</span>
        </div>
      : null}
      {startup?.phase === "project-review" ?
        <Button onClick={() => void onInitialize()}>Use this project</Button>
      : null}
      <details>
        <summary>Project details</summary>
        <div className="project-detail-content">
          <small>Platform: {readiness.platform_id}</small>
          <small>Project: {readiness.project_root}</small>
          <ul className="readiness-checks">
            {readiness.checks.map((check) => (
              <li data-status={check.status} key={check.check_id}>
                <span>{check.summary}</span>
                {check.status === "pass" ? null : (
                  <small>
                    {check.next_action ?? "Review this check and try again."}
                  </small>
                )}
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

const localModelOptions = (catalog: ModelArtifacts): LocalModelChoice[] =>
  catalog.models;

const localComputeLabel = (runtime: LocalModelChoice["runtime"]): string =>
  runtime === "vllm" ? "Requires an NVIDIA GPU" : "Runs on CPU";

const CustomLocalModelSetup = ({
  downloads,
  onDownload,
  onImport,
  onInspect,
}: {
  downloads: ModelArtifacts["downloads"];
  onDownload: (request: CustomLocalModelDownloadRequest) => Promise<void>;
  onImport: (request: LocalModelImportRequest) => Promise<void>;
  onInspect: (request: ModelRepositoryRequest) => Promise<ModelRepositoryPlan>;
}) => {
  const modelIssueUrl =
    "https://github.com/SchmiedmayerLab/heartwood/issues/new/choose";
  const [repository, setRepository] = useState("");
  const [revision, setRevision] = useState("");
  const [plan, setPlan] = useState<ModelRepositoryPlan | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [importPath, setImportPath] = useState("");
  const [importRepository, setImportRepository] = useState("");
  const [importRevision, setImportRevision] = useState("");
  const [importLicense, setImportLicense] = useState("");
  const [importComplete, setImportComplete] = useState(false);
  const modelDownload =
    plan === null ? undefined : (
      downloads.find((item) => item.model_id === plan.model.model_id)
    );

  const inspect = async () => {
    if (!repository.trim()) return;
    setPending(true);
    setError(null);
    setPlan(null);
    try {
      setPlan(
        await onInspect({
          repository: repository.trim(),
          ...(revision.trim() ? { revision: revision.trim() } : {}),
        }),
      );
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setPending(false);
    }
  };

  const download = async () => {
    if (!plan) return;
    setPending(true);
    setError(null);
    try {
      await onDownload({
        repository: plan.model.source_repository,
        revision: plan.model.source_revision,
      });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setPending(false);
    }
  };

  const importModel = async () => {
    if (
      !importPath.trim() ||
      !importRepository.trim() ||
      !importRevision.trim() ||
      !importLicense.trim()
    )
      return;
    setPending(true);
    setError(null);
    setImportComplete(false);
    try {
      await onImport({
        path: importPath.trim(),
        repository: importRepository.trim(),
        revision: importRevision.trim(),
        license: importLicense.trim(),
      });
      setImportComplete(true);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setPending(false);
    }
  };

  return (
    <details className="advanced-section local-model-import">
      <summary>Other model</summary>
      <div className="advanced-section-content local-model-form">
        <p className="panel-empty">
          Enter a public or authorized Hugging Face model identifier. Heartwood
          will select a supported runtime and model file for this deployment.
        </p>
        <label>
          Model repository
          <Input
            autoComplete="off"
            placeholder="owner/model"
            value={repository}
            onChange={(event) => {
              setRepository(event.target.value);
              setPlan(null);
              setError(null);
            }}
          />
        </label>
        <details className="model-plan-source">
          <summary>Version options</summary>
          <label>
            Model revision
            <Input
              autoComplete="off"
              placeholder="Optional tag, branch, or commit"
              value={revision}
              onChange={(event) => {
                setRevision(event.target.value);
                setPlan(null);
                setError(null);
              }}
            />
          </label>
        </details>
        <Button
          disabled={pending || !repository.trim()}
          isPending={pending && plan === null}
          variant="outline"
          onClick={() => void inspect()}
        >
          <Search size={15} />
          Check model
        </Button>
        {plan ?
          <div className="local-model-plan" role="status">
            <strong>{plan.model.label}</strong>
            <span>
              {localComputeLabel(plan.model.runtime)} ·{" "}
              {formatBytes(plan.model.size_bytes)} · Up to{" "}
              {plan.model.context_window.toLocaleString()} tokens
            </span>
            <small>{plan.selection_reason}</small>
            <small>{plan.model.purpose}</small>
            {plan.model.minimum_resource_envelope ?
              <small>{plan.model.minimum_resource_envelope}</small>
            : null}
            {plan.model.recommended_resource_envelope ?
              <small>{plan.model.recommended_resource_envelope}</small>
            : null}
            <small>{plan.model.license_posture}</small>
            <details className="model-plan-source">
              <summary>Source details</summary>
              <div>
                <small>Repository: {plan.model.source_repository}</small>
                <small>Revision: {plan.model.source_revision}</small>
                <small>
                  Representation:{" "}
                  {plan.model.source_path ?? "Complete repository snapshot"}
                </small>
              </div>
            </details>
            <ArtifactDownloadStatus
              alias={plan.model.label}
              download={modelDownload}
            />
            <Button
              disabled={
                pending ||
                modelDownload?.status === "downloading" ||
                modelDownload?.status === "ready"
              }
              isPending={pending || modelDownload?.status === "downloading"}
              onClick={() => void download()}
            >
              {modelDownload?.status === "ready" ?
                <Check size={15} />
              : <Download size={15} />}
              {modelDownload?.status === "ready" ?
                "Downloaded"
              : "Download model"}
            </Button>
          </div>
        : null}
        <details className="model-plan-source">
          <summary>Import an existing model</summary>
          <div className="local-model-form">
            <p className="panel-empty">
              Use a GGUF file or vLLM model directory already visible to this
              Heartwood server. The files are copied into this project.
            </p>
            <label>
              Server path
              <Input
                value={importPath}
                onChange={(event) => setImportPath(event.target.value)}
              />
            </label>
            <label>
              Source repository
              <Input
                placeholder="owner/model"
                value={importRepository}
                onChange={(event) => setImportRepository(event.target.value)}
              />
            </label>
            <label>
              Source revision
              <Input
                placeholder="Immutable commit hash"
                value={importRevision}
                onChange={(event) => setImportRevision(event.target.value)}
              />
            </label>
            <label>
              License
              <Input
                placeholder="For example, Apache-2.0"
                value={importLicense}
                onChange={(event) => setImportLicense(event.target.value)}
              />
            </label>
            <Button
              disabled={
                pending ||
                !importPath.trim() ||
                !importRepository.trim() ||
                !importRevision.trim() ||
                !importLicense.trim()
              }
              isPending={pending}
              variant="outline"
              onClick={() => void importModel()}
            >
              Import model
            </Button>
            {importComplete ?
              <span role="status">Model imported and selected</span>
            : null}
          </div>
        </details>
        {error ?
          <div className="connection-error" role="alert">
            <span>{error}</span>
            {error.includes(modelIssueUrl) ?
              <a href={modelIssueUrl} rel="noreferrer" target="_blank">
                Report an unsupported model
              </a>
            : null}
          </div>
        : null}
      </div>
    </details>
  );
};

const ModelConnectionSetup = ({
  settings,
  onConnect,
  onConfigureSource,
  onDiscover,
  onForgetCredential,
}: {
  settings: ModelSettings | null;
  onConnect: (request: ModelConnectRequest) => Promise<void>;
  onConfigureSource: (sourceId: ModelSource) => Promise<ModelSettings>;
  onDiscover: (request: ModelCatalogRequest) => Promise<ModelCatalog>;
  onForgetCredential: (connectionId: string) => Promise<void>;
}) => {
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [selectedModel, setSelectedModel] = useState("");
  const [token, setToken] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [manualModel, setManualModel] = useState("");
  const [remember, setRemember] = useState(false);
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
    setRemember(false);
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
        ...(remember ? { remember: true } : {}),
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
          <h3>Available connections</h3>
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
                    <>
                      <ModelConnectionForm
                        baseUrl={baseUrl}
                        catalog={catalog}
                        connection={connection}
                        error={error}
                        manualModel={manualModel}
                        pending={pending}
                        selectedModel={selectedModel}
                        token={token}
                        remember={remember}
                        rememberAvailable={
                          settings?.credential_store.persistence_available ===
                            true && connection.connection_id !== "custom-api"
                        }
                        onActivate={activateModel}
                        onBaseUrl={setBaseUrl}
                        onDiscover={discover}
                        onManualModel={setManualModel}
                        onRemember={setRemember}
                        onSelectedModel={setSelectedModel}
                        onToken={setToken}
                      />
                      {(
                        settings?.credential_bindings.some(
                          (binding) =>
                            binding.binding_id === item.api_key_env &&
                            ["keyring", "process"].includes(
                              binding.source ?? "",
                            ),
                        )
                      ) ?
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            void onForgetCredential(item.connection_id)
                          }
                        >
                          Forget token
                        </Button>
                      : null}
                    </>
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
  remember: boolean;
  rememberAvailable: boolean;
  onActivate: (manual: boolean) => Promise<void>;
  onBaseUrl: (value: string) => void;
  onDiscover: () => Promise<void>;
  onManualModel: (value: string) => void;
  onSelectedModel: (value: string) => void;
  onToken: (value: string) => void;
  onRemember: (value: boolean) => void;
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
  remember,
  rememberAvailable,
  onActivate,
  onBaseUrl,
  onDiscover,
  onManualModel,
  onSelectedModel,
  onToken,
  onRemember,
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
    {rememberAvailable && token.trim() ?
      <label className="checkbox-row">
        <Checkbox
          checked={remember}
          onCheckedChange={(checked) => onRemember(checked === true)}
        />
        Remember securely for this project
      </label>
    : null}
    {(
      connection.accepts_token &&
      (connection.credential_status === "missing" ||
        connection.connection_id === "custom-api")
    ) ?
      <label>
        {connection.connection_id === "custom-api" ?
          "Token (optional for loopback services)"
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

const connectionGroups = (connections: ModelConnection[]) => {
  const groups = new Map<
    ModelConnection["group"],
    { label: string; connections: ModelConnection[] }
  >();
  connections.forEach((connection) => {
    const existing = groups.get(connection.group);
    if (existing) {
      existing.connections.push(connection);
      return;
    }
    groups.set(connection.group, {
      label: connection.group_label,
      connections: [connection],
    });
  });
  return [...groups.values()];
};

const ConnectionIcon = ({ connection }: { connection: ModelConnection }) => {
  if (connection.connection_id === "heartwood") return <HardDrive size={16} />;
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
  if (connection.connection_id === "heartwood") return "Managed by Heartwood";
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
