/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Badge } from "@stanfordspezi/spezi-web-design-system/components/Badge";
import { Button } from "@stanfordspezi/spezi-web-design-system/components/Button";
import { Input } from "@stanfordspezi/spezi-web-design-system/components/Input";
import { Textarea } from "@stanfordspezi/spezi-web-design-system/components/Textarea";
import { SpeziProvider } from "@stanfordspezi/spezi-web-design-system/SpeziProvider";
import {
  Activity,
  Ban,
  BookOpen,
  Check,
  Database,
  Download,
  Pause,
  RotateCcw,
  Send,
  Settings,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { GatewayClient, createCommand, type HeartwoodClient } from "./client";
import type {
  ActionConfirmationMode,
  ActionSettings,
  ApprovalControl,
  ConversationMessage,
  CredentialKind,
  JsonValue,
  ModelArtifacts,
  ModelProfile,
  ModelSettings as ModelSettingsState,
  ModelValidation,
  SessionEvent,
  SkillSettings,
  SkillSummary,
} from "./types";
import { buildViewModel } from "./viewModel";

interface AppProps {
  client?: HeartwoodClient;
  initialSessionId?: string;
}

interface LocalConversationMessage extends ConversationMessage {
  sessionId: string;
}

type SidePanel = "activity" | "settings" | "skills" | null;

const emptyProfile = (): ModelProfile => ({
  profile_id: "local",
  model: "openai/",
  policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
  capability_tier: "supervised",
  base_url: "http://127.0.0.1:8765/v1",
  credential_kind: "none",
  api_key_env: null,
  api_key_file: null,
  api_version: null,
  aws_region_name: null,
  aws_profile_name: null,
  description: null,
});

export const App = ({
  client,
  initialSessionId = "session-local",
}: AppProps) => {
  const [sessionId, setSessionId] = useState(initialSessionId);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [prompt, setPrompt] = useState("");
  const [localConversation, setLocalConversation] = useState<
    LocalConversationMessage[]
  >([]);
  const [status, setStatus] = useState<"idle" | "busy" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [sidePanel, setSidePanel] = useState<SidePanel>(null);
  const [modelSettings, setModelSettings] = useState<ModelSettingsState | null>(
    null,
  );
  const [actionSettings, setActionSettings] = useState<ActionSettings | null>(
    null,
  );
  const [modelArtifacts, setModelArtifacts] = useState<ModelArtifacts | null>(
    null,
  );
  const [profileDraft, setProfileDraft] = useState<ModelProfile>(emptyProfile);
  const [validation, setValidation] = useState<ModelValidation | null>(null);
  const [skillSettings, setSkillSettings] = useState<SkillSettings | null>(
    null,
  );
  const [skillCandidate, setSkillCandidate] = useState<SkillSummary | null>(
    null,
  );
  const [skillSource, setSkillSource] = useState("");
  const [skillApproved, setSkillApproved] = useState(false);
  const conversationEndRef = useRef<HTMLDivElement | null>(null);
  const resolvedClient = useMemo(() => client ?? new GatewayClient(), [client]);
  const viewModel = useMemo(() => buildViewModel(events), [events]);
  const conversation = useMemo(
    () =>
      mergeConversationMessages(
        localConversation.filter((message) => message.sessionId === sessionId),
        viewModel.conversation.filter((message) => message.role !== "trace"),
      ),
    [localConversation, sessionId, viewModel.conversation],
  );
  const pendingActions = viewModel.approvalControls.filter(
    (control) =>
      control.targetType === "tool-call" && control.decision === null,
  );

  useEffect(() => {
    let active = true;
    resolvedClient
      .replayEvents(sessionId)
      .then(({ events: replayed }) => {
        if (active) {
          setEvents(replayed);
          setStatus("idle");
        }
      })
      .catch((caught: unknown) => {
        if (active) {
          setError(errorMessage(caught));
          setStatus("error");
        }
      });
    const cleanup = resolvedClient.streamEvents(
      sessionId,
      undefined,
      (streamed) => setEvents((current) => mergeEvents(current, streamed)),
    );
    return () => {
      active = false;
      cleanup();
    };
  }, [resolvedClient, sessionId]);

  useEffect(() => {
    void resolvedClient
      .getActionSettings()
      .then(setActionSettings)
      .catch((caught: unknown) => setError(errorMessage(caught)));
    void resolvedClient
      .getModelSettings()
      .then(setModelSettings)
      .catch((caught: unknown) => setError(errorMessage(caught)));
    void resolvedClient
      .getModelArtifacts()
      .then(setModelArtifacts)
      .catch((caught: unknown) => setError(errorMessage(caught)));
    void resolvedClient
      .getSkillSettings()
      .then(setSkillSettings)
      .catch((caught: unknown) => setError(errorMessage(caught)));
  }, [resolvedClient]);

  useEffect(() => {
    scrollConversationEnd(conversationEndRef.current);
  }, [conversation.length]);

  const send = async (
    kind: Parameters<typeof createCommand>[1],
    payload: Record<string, JsonValue> = {},
  ) => {
    setStatus("busy");
    setError(null);
    try {
      const command = createCommand(sessionId, kind, events.length, payload);
      const submittedPrompt = promptContent(payload);
      if ((kind === "chat" || kind === "run") && submittedPrompt) {
        setLocalConversation((current) => [
          ...current,
          {
            id: `local-${command.command_id}`,
            sequence: (events.at(-1)?.sequence ?? -1) + 0.5,
            role: "user",
            label: "You",
            content: submittedPrompt,
            detail: null,
            sessionId,
          },
        ]);
      }
      const response = await resolvedClient.postCommand(command);
      setEvents((current) => mergeEvents(current, response.events));
      setStatus("idle");
    } catch (caught) {
      setError(errorMessage(caught));
      setStatus("error");
    }
  };

  const submitPrompt = () => {
    const value = prompt.trim();
    if (!value || status === "busy") {
      return;
    }
    setPrompt("");
    void send("chat", { prompt: value });
  };

  const decideAction = (kind: "approve" | "deny", control: ApprovalControl) =>
    send(kind, {
      target_id: control.targetId,
      target_type: "tool-call",
    });

  const refreshSettings = async () => {
    const [actions, settings, artifacts] = await Promise.all([
      resolvedClient.getActionSettings(),
      resolvedClient.getModelSettings(),
      resolvedClient.getModelArtifacts(),
    ]);
    setActionSettings(actions);
    setModelSettings(settings);
    setModelArtifacts(artifacts);
  };

  const selectActionMode = async (mode: ActionConfirmationMode) => {
    setError(null);
    try {
      setActionSettings(
        await resolvedClient.selectActionConfirmationMode(mode),
      );
      setValidation(null);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const saveProfile = async () => {
    setError(null);
    try {
      const settings = await resolvedClient.saveModelProfile(profileDraft);
      setModelSettings(settings);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const selectProfile = async (profileId: string) => {
    setError(null);
    try {
      setModelSettings(await resolvedClient.selectModelProfile(profileId));
      setValidation(await resolvedClient.validateModelProfile(profileId));
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  return (
    <SpeziProvider
      router={{
        Link: ({ href, ...props }) => <a href={href ?? "#"} {...props} />,
      }}
    >
      <main className="app-shell">
        <header className="topbar">
          <div className="brand-block">
            <h1>Heartwood</h1>
            <div className="session-row">
              <Input
                aria-label="Session ID"
                value={sessionId}
                onChange={(event) => setSessionId(event.target.value)}
              />
              <Badge
                variant={status === "error" ? "destructiveLight" : "secondary"}
              >
                {status}
              </Badge>
            </div>
          </div>
          <nav aria-label="Session tools" className="topbar-actions">
            <Button
              aria-label="Detect environment"
              size="sm"
              title="Detect environment"
              variant="outline"
              onClick={() => void send("detect")}
            >
              <Database size={17} />
            </Button>
            <Button
              aria-label="Skills"
              size="sm"
              title="Skills"
              variant={sidePanel === "skills" ? "secondary" : "outline"}
              onClick={() =>
                setSidePanel(sidePanel === "skills" ? null : "skills")
              }
            >
              <BookOpen size={17} />
            </Button>
            <Button
              aria-label="Show activity"
              size="sm"
              title="Show activity"
              variant={sidePanel === "activity" ? "secondary" : "outline"}
              onClick={() =>
                setSidePanel(sidePanel === "activity" ? null : "activity")
              }
            >
              <Activity size={17} />
            </Button>
            <Button
              aria-label="Settings"
              size="sm"
              title="Settings"
              variant={sidePanel === "settings" ? "secondary" : "outline"}
              onClick={() =>
                setSidePanel(sidePanel === "settings" ? null : "settings")
              }
            >
              <Settings size={17} />
            </Button>
            <Button
              aria-label="Export audit"
              size="sm"
              title="Export audit"
              variant="outline"
              onClick={() => void send("audit.export")}
            >
              <Download size={17} />
            </Button>
          </nav>
        </header>

        {error ?
          <div className="error-banner">{error}</div>
        : null}

        <div className={`agent-layout ${sidePanel === null ? "single" : ""}`}>
          <section
            className="conversation-workspace"
            aria-label="Agent conversation"
          >
            <div
              aria-label="Conversation transcript"
              className="conversation-list"
              role="log"
            >
              {conversation.length === 0 ?
                <div className="conversation-empty">
                  <ShieldCheck size={22} />
                  <span>Start a research task</span>
                </div>
              : conversation.map((message) => (
                  <article
                    className={`conversation-message ${message.role}`}
                    key={message.id}
                  >
                    <div className="conversation-meta">
                      <small>{message.label}</small>
                      {message.detail ?
                        <span>{message.detail}</span>
                      : null}
                    </div>
                    <p>{message.content}</p>
                  </article>
                ))
              }
              <div ref={conversationEndRef} aria-hidden="true" />
            </div>

            <div className="composer-area">
              {pendingActions.map((control) => (
                <div className="pending-action" key={control.targetId}>
                  <div>
                    <small>Pending action</small>
                    <strong>{control.label}</strong>
                  </div>
                  <div className="pending-actions">
                    <Button
                      aria-label={`Allow ${control.targetId}`}
                      size="sm"
                      onClick={() => void decideAction("approve", control)}
                    >
                      <Check size={16} />
                      Allow once
                    </Button>
                    <Button
                      aria-label={`Reject ${control.targetId}`}
                      size="sm"
                      variant="outline"
                      onClick={() => void decideAction("deny", control)}
                    >
                      <Ban size={16} />
                      Reject
                    </Button>
                  </div>
                </div>
              ))}
              <div className="composer">
                <Textarea
                  aria-label="Task"
                  placeholder="Ask Heartwood to inspect, analyze, or change the workspace"
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      submitPrompt();
                    }
                  }}
                />
                <div className="composer-actions">
                  <Button
                    aria-label="Pause agent"
                    size="sm"
                    title="Pause agent"
                    variant="outline"
                    onClick={() => void send("pause")}
                  >
                    <Pause size={17} />
                  </Button>
                  <Button
                    aria-label="Send task"
                    disabled={!prompt.trim()}
                    isPending={status === "busy"}
                    size="sm"
                    title="Send task"
                    onClick={submitPrompt}
                  >
                    <Send size={17} />
                  </Button>
                </div>
              </div>
            </div>
          </section>

          {sidePanel === "activity" ?
            <ActivityPanel
              events={events}
              onClose={() => setSidePanel(null)}
              onReplay={() =>
                void resolvedClient
                  .replayEvents(sessionId)
                  .then(({ events: replayed }) => setEvents(replayed))
              }
            />
          : null}
          {sidePanel === "skills" ?
            <SkillsPanel
              approved={skillApproved}
              candidate={skillCandidate}
              settings={skillSettings}
              source={skillSource}
              onApproved={setSkillApproved}
              onClose={() => setSidePanel(null)}
              onInspect={() =>
                void resolvedClient
                  .inspectSkill(skillSource.trim())
                  .then((summary) => {
                    setSkillCandidate(summary);
                    setSkillApproved(false);
                  })
                  .catch((caught: unknown) => setError(errorMessage(caught)))
              }
              onInstall={() =>
                void resolvedClient
                  .installSkill(skillSource.trim())
                  .then((settings) => {
                    setSkillSettings(settings);
                    setSkillCandidate(null);
                    setSkillApproved(false);
                    setSkillSource("");
                  })
                  .catch((caught: unknown) => setError(errorMessage(caught)))
              }
              onRemove={(name) =>
                void resolvedClient
                  .removeSkill(name)
                  .then(setSkillSettings)
                  .catch((caught: unknown) => setError(errorMessage(caught)))
              }
              onSource={setSkillSource}
            />
          : null}
          {sidePanel === "settings" ?
            <SettingsPanel
              actions={actionSettings}
              artifacts={modelArtifacts}
              draft={profileDraft}
              settings={modelSettings}
              validation={validation}
              onClose={() => setSidePanel(null)}
              onDraft={setProfileDraft}
              onDownload={(artifactId) =>
                void resolvedClient
                  .downloadModelArtifact(artifactId)
                  .then((download) =>
                    setModelArtifacts((current) =>
                      current === null ? current : (
                        {
                          ...current,
                          downloads: [
                            ...current.downloads.filter(
                              (item) => item.artifact_id !== artifactId,
                            ),
                            download,
                          ],
                        }
                      ),
                    ),
                  )
                  .catch((caught: unknown) => setError(errorMessage(caught)))
              }
              onRefresh={() => void refreshSettings()}
              onRemove={(profileId) =>
                void resolvedClient
                  .removeModelProfile(profileId)
                  .then(setModelSettings)
                  .catch((caught: unknown) => setError(errorMessage(caught)))
              }
              onSave={() => void saveProfile()}
              onSelectActionMode={(mode) => void selectActionMode(mode)}
              onSelect={(profileId) => void selectProfile(profileId)}
              onValidate={(profileId) =>
                void resolvedClient
                  .validateModelProfile(profileId)
                  .then(setValidation)
                  .catch((caught: unknown) => setError(errorMessage(caught)))
              }
            />
          : null}
        </div>
      </main>
    </SpeziProvider>
  );
};

interface SkillsPanelProps {
  approved: boolean;
  candidate: SkillSummary | null;
  settings: SkillSettings | null;
  source: string;
  onApproved: (approved: boolean) => void;
  onClose: () => void;
  onInspect: () => void;
  onInstall: () => void;
  onRemove: (name: string) => void;
  onSource: (source: string) => void;
}

const SkillsPanel = ({
  approved,
  candidate,
  settings,
  source,
  onApproved,
  onClose,
  onInspect,
  onInstall,
  onRemove,
  onSource,
}: SkillsPanelProps) => (
  <aside aria-label="Skills" className="side-panel settings-panel">
    <div className="side-panel-header">
      <h2>Skills</h2>
      <Button
        aria-label="Close Skills"
        size="sm"
        variant="outline"
        onClick={onClose}
      >
        <X size={16} />
      </Button>
    </div>
    <section className="settings-section skill-list">
      <h3>Available</h3>
      {settings?.skills.map((skill) => (
        <div className="skill-row" key={`${skill.source}-${skill.name}`}>
          <div>
            <strong>{skill.name}</strong>
            <span>
              {skill.trust_tier} · {skill.source}
            </span>
          </div>
          {skill.source === "installed" ?
            <Button
              aria-label={`Remove ${skill.name}`}
              size="sm"
              title={`Remove ${skill.name}`}
              variant="outline"
              onClick={() => onRemove(skill.name)}
            >
              <Trash2 size={15} />
            </Button>
          : null}
        </div>
      ))}
    </section>
    <section className="settings-section skill-installer">
      <h3>Install extension</h3>
      <label>
        Mounted source directory
        <Input
          value={source}
          onChange={(event) => onSource(event.target.value)}
        />
      </label>
      <Button disabled={!source.trim()} variant="outline" onClick={onInspect}>
        Inspect
      </Button>
      {candidate ?
        <div className="skill-review">
          <strong>{candidate.name}</strong>
          <span>{candidate.approval_summary}</span>
          <span>Tools: {candidate.declared_tools.join(", ")}</span>
          <label className="checkbox-control">
            <input
              checked={approved}
              type="checkbox"
              onChange={(event) => onApproved(event.target.checked)}
            />
            Approve this installation
          </label>
          <Button disabled={!approved} onClick={onInstall}>
            Install
          </Button>
        </div>
      : null}
    </section>
  </aside>
);

interface ActivityPanelProps {
  events: SessionEvent[];
  onClose: () => void;
  onReplay: () => void;
}

const ActivityPanel = ({ events, onClose, onReplay }: ActivityPanelProps) => {
  const viewModel = buildViewModel(events);
  return (
    <aside aria-label="Activity" className="side-panel">
      <div className="side-panel-header">
        <h2>Activity</h2>
        <div>
          <Button
            aria-label="Replay events"
            size="sm"
            variant="outline"
            onClick={onReplay}
          >
            <RotateCcw size={16} />
          </Button>
          <Button
            aria-label="Close activity"
            size="sm"
            variant="outline"
            onClick={onClose}
          >
            <X size={16} />
          </Button>
        </div>
      </div>
      <div className="activity-list">
        {viewModel.activity.map((item) => (
          <div className="activity-row" key={`${item.sequence}-${item.kind}`}>
            <small>{String(item.sequence).padStart(3, "0")}</small>
            <div>
              <strong>{item.label}</strong>
              <span>{item.detail}</span>
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
};

interface SettingsPanelProps {
  actions: ActionSettings | null;
  artifacts: ModelArtifacts | null;
  draft: ModelProfile;
  settings: ModelSettingsState | null;
  validation: ModelValidation | null;
  onClose: () => void;
  onDraft: (profile: ModelProfile) => void;
  onDownload: (artifactId: string) => void;
  onRefresh: () => void;
  onRemove: (profileId: string) => void;
  onSave: () => void;
  onSelectActionMode: (mode: ActionConfirmationMode) => void;
  onSelect: (profileId: string) => void;
  onValidate: (profileId?: string) => void;
}

const SettingsPanel = ({
  actions,
  artifacts,
  draft,
  settings,
  validation,
  onClose,
  onDraft,
  onDownload,
  onRefresh,
  onRemove,
  onSave,
  onSelectActionMode,
  onSelect,
  onValidate,
}: SettingsPanelProps) => {
  const applyPreset = (presetId: string) => {
    const preset = settings?.presets.find(
      (item) => item.preset_id === presetId,
    );
    if (!preset) return;
    onDraft({
      ...draft,
      model: preset.model_prefix,
      base_url: preset.base_url,
      policy_endpoint: preset.policy_endpoint ?? draft.policy_endpoint,
      credential_kind: preset.credential_kind,
      api_key_env: preset.api_key_env,
      api_key_file: null,
      description: preset.description,
    });
  };
  return (
    <aside aria-label="Settings" className="side-panel settings-panel">
      <div className="side-panel-header">
        <h2>Settings</h2>
        <div>
          <Button
            aria-label="Refresh settings"
            size="sm"
            variant="outline"
            onClick={onRefresh}
          >
            <RotateCcw size={16} />
          </Button>
          <Button
            aria-label="Close settings"
            size="sm"
            variant="outline"
            onClick={onClose}
          >
            <X size={16} />
          </Button>
        </div>
      </div>

      <section className="settings-section">
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

      <section className="settings-section">
        <h3>Active profile</h3>
        <div className="inline-control">
          <select
            aria-label="Active model profile"
            value={settings?.active_profile ?? ""}
            onChange={(event) => onSelect(event.target.value)}
          >
            <option disabled value="">
              Not configured
            </option>
            {settings?.profiles.map((profile) => (
              <option key={profile.profile_id} value={profile.profile_id}>
                {profile.profile_id}
              </option>
            ))}
          </select>
          <Button
            aria-label="Validate active model profile"
            size="sm"
            variant="outline"
            onClick={() => onValidate(settings?.active_profile ?? undefined)}
          >
            <ShieldCheck size={16} />
          </Button>
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
          </div>
        : null}
      </section>

      <section className="settings-section profile-list">
        <h3>Profiles</h3>
        {settings?.profiles.map((profile) => (
          <div className="profile-row" key={profile.profile_id}>
            <button type="button" onClick={() => onDraft(profile)}>
              <strong>{profile.profile_id}</strong>
              <span>{profile.model}</span>
            </button>
            <Button
              aria-label={`Remove ${profile.profile_id}`}
              size="sm"
              title={`Remove ${profile.profile_id}`}
              variant="outline"
              onClick={() => onRemove(profile.profile_id)}
            >
              <Trash2 size={15} />
            </Button>
          </div>
        ))}
      </section>

      <section className="settings-section artifact-list">
        <h3>Local artifacts</h3>
        {artifacts?.artifacts.map((artifact) => {
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
              <Button
                aria-label={`Download ${artifact.model_alias}`}
                disabled={download?.status === "downloading"}
                isPending={download?.status === "downloading"}
                size="sm"
                title={`Download ${artifact.model_alias}`}
                variant="outline"
                onClick={() => onDownload(artifact.artifact_id)}
              >
                <Download size={15} />
              </Button>
            </div>
          );
        })}
      </section>

      <section className="settings-section profile-editor">
        <h3>Profile</h3>
        <label>
          Preset
          <select
            aria-label="Provider preset"
            defaultValue=""
            onChange={(event) => applyPreset(event.target.value)}
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
            onChange={(event) =>
              onDraft({ ...draft, model: event.target.value })
            }
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
                onDraft({
                  ...draft,
                  api_key_env: nullIfEmpty(event.target.value),
                })
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
                onDraft({
                  ...draft,
                  api_key_file: nullIfEmpty(event.target.value),
                })
              }
            />
          </label>
        : null}
        <Button onClick={onSave}>Save profile</Button>
      </section>
    </aside>
  );
};

const nullIfEmpty = (value: string): string | null => value.trim() || null;

const formatBytes = (value: number): string => {
  const gibibytes = value / 1024 ** 3;
  if (gibibytes >= 1) return `${gibibytes.toFixed(1)} GiB`;
  return `${(value / 1024 ** 2).toFixed(0)} MiB`;
};

const promptContent = (payload: Record<string, JsonValue>): string => {
  const value = payload.prompt;
  return typeof value === "string" ? value.trim() : "";
};

const mergeConversationMessages = (
  localMessages: ConversationMessage[],
  eventMessages: ConversationMessage[],
): ConversationMessage[] => {
  const messages = new Map<string, ConversationMessage>();
  for (const message of [...localMessages, ...eventMessages]) {
    messages.set(message.id, message);
  }
  return [...messages.values()].sort(
    (left, right) =>
      left.sequence - right.sequence || left.id.localeCompare(right.id),
  );
};

const scrollConversationEnd = (target: unknown): void => {
  if (hasScrollIntoView(target)) target.scrollIntoView({ block: "end" });
};

const hasScrollIntoView = (
  value: unknown,
): value is { scrollIntoView: (options: ScrollIntoViewOptions) => void } =>
  typeof value === "object" &&
  value !== null &&
  "scrollIntoView" in value &&
  typeof value.scrollIntoView === "function";

const mergeEvents = (
  current: SessionEvent[],
  next: SessionEvent[],
): SessionEvent[] => {
  const events = new Map(current.map((event) => [event.event_id, event]));
  for (const event of next) events.set(event.event_id, event);
  return [...events.values()].sort(
    (left, right) => left.sequence - right.sequence,
  );
};

const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);
