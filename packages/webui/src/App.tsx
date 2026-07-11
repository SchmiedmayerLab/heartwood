/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Button } from "@stanfordspezi/spezi-web-design-system/components/Button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from "@stanfordspezi/spezi-web-design-system/components/Sheet";
import { SpeziProvider } from "@stanfordspezi/spezi-web-design-system/SpeziProvider";
import { X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { GatewayClient, createCommand, type HeartwoodClient } from "./client";
import { ConversationWorkspace } from "./components/ConversationWorkspace";
import {
  SessionRail,
  SessionRailContent,
  type UtilityPanel,
} from "./components/SessionRail";
import { UtilitySheet } from "./components/UtilitySheet";
import { WorkspaceHeader } from "./components/WorkspaceHeader";
import { modelProfileLabel } from "./modelPresentation";
import type {
  ActionConfirmationMode,
  ActionSettings,
  ApprovalControl,
  ConversationMessage,
  JsonValue,
  ModelArtifacts,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelProfile,
  ModelSettings,
  ModelValidation,
  SessionEvent,
  SessionSummary,
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

interface InitialState {
  selectedSessionId: string;
  sessions: SessionSummary[];
}

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

export const App = ({ client, initialSessionId }: AppProps) => {
  const resolvedClient = useMemo(() => client ?? new GatewayClient(), [client]);
  const initialization = useRef<Promise<InitialState> | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [prompt, setPrompt] = useState("");
  const [localConversation, setLocalConversation] = useState<
    LocalConversationMessage[]
  >([]);
  const [requestStatus, setRequestStatus] = useState<"idle" | "busy" | "error">(
    "idle",
  );
  const [error, setError] = useState<string | null>(null);
  const [panel, setPanel] = useState<UtilityPanel>(null);
  const [mobileSessionsOpen, setMobileSessionsOpen] = useState(false);
  const [modelSettings, setModelSettings] = useState<ModelSettings | null>(
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
  const [validationFailureKey, setValidationFailureKey] = useState<
    string | null
  >(null);
  const [skillSettings, setSkillSettings] = useState<SkillSettings | null>(
    null,
  );
  const [skillCandidate, setSkillCandidate] = useState<SkillSummary | null>(
    null,
  );
  const [skillSource, setSkillSource] = useState("");
  const [skillApproved, setSkillApproved] = useState(false);
  const conversationEndRef = useRef<HTMLDivElement | null>(null);
  const selectionGeneration = useRef(0);
  const utilityTriggerRef = useRef<HTMLElement | null>(null);

  const refreshSessions = useCallback(async () => {
    const response = await resolvedClient.listSessions();
    setSessions(response.sessions);
    return response.sessions;
  }, [resolvedClient]);

  useEffect(() => {
    let active = true;
    const generation = selectionGeneration.current;
    initialization.current ??= initializeSessions(
      resolvedClient,
      initialSessionId,
    );
    void initialization.current
      .then((state) => {
        if (!active || selectionGeneration.current !== generation) return;
        setSessions(state.sessions);
        setSessionId(state.selectedSessionId);
      })
      .catch((caught: unknown) => {
        if (!active || selectionGeneration.current !== generation) return;
        setError(errorMessage(caught));
        setRequestStatus("error");
      });
    return () => {
      active = false;
    };
  }, [initialSessionId, resolvedClient]);

  useEffect(() => {
    if (sessionId === null) return;
    let active = true;
    let refreshTimer: number | null = null;
    resolvedClient
      .replayEvents(sessionId)
      .then(({ events: replayed }) => {
        if (active) {
          setEvents(replayed);
          setRequestStatus("idle");
        }
      })
      .catch((caught: unknown) => {
        if (active) {
          setError(errorMessage(caught));
          setRequestStatus("error");
        }
      });
    const cleanup = resolvedClient.streamEvents(
      sessionId,
      undefined,
      (streamed) => {
        if (!active) return;
        setEvents((current) => mergeEvents(current, streamed));
        refreshTimer ??= window.setTimeout(() => {
          refreshTimer = null;
          void refreshSessions().catch((caught: unknown) =>
            setError(errorMessage(caught)),
          );
        }, 250);
      },
    );
    return () => {
      active = false;
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      cleanup();
    };
  }, [refreshSessions, resolvedClient, sessionId]);

  useEffect(() => {
    void Promise.all([
      resolvedClient.getActionSettings(),
      resolvedClient.getModelSettings(),
      resolvedClient.getModelArtifacts(),
      resolvedClient.getSkillSettings(),
    ])
      .then(([actions, models, artifacts, skills]) => {
        setActionSettings(actions);
        setModelSettings(models);
        setModelArtifacts(artifacts);
        setSkillSettings(skills);
      })
      .catch((caught: unknown) => setError(errorMessage(caught)));
  }, [resolvedClient]);

  const modelDownloadActive =
    modelArtifacts?.downloads.some(
      (download) => download.status === "downloading",
    ) ?? false;

  useEffect(() => {
    if (!modelDownloadActive) return;
    let active = true;
    let timer: number | null = null;
    const poll = async (): Promise<void> => {
      try {
        const artifacts = await resolvedClient.getModelArtifacts();
        if (!active) return;
        setModelArtifacts(artifacts);
        if (artifacts.downloads.some((item) => item.status === "downloading")) {
          timer = window.setTimeout(() => void poll(), 500);
        }
      } catch (caught) {
        if (active) setError(errorMessage(caught));
      }
    };
    timer = window.setTimeout(() => void poll(), 500);
    return () => {
      active = false;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [modelDownloadActive, resolvedClient]);

  const viewModel = useMemo(() => buildViewModel(events), [events]);
  const selectedSession = useMemo(
    () => sessions.find((session) => session.session_id === sessionId) ?? null,
    [sessionId, sessions],
  );
  const activeProfile = useMemo(
    () =>
      modelSettings?.profiles.find(
        (profile) => profile.profile_id === modelSettings.active_profile,
      ) ?? null,
    [modelSettings],
  );
  const activeValidation =
    (
      validation !== null &&
      activeProfile !== null &&
      validation.profile.profile_id === activeProfile.profile_id &&
      validation.profile.model === activeProfile.model &&
      validation.action_confirmation_mode === actionSettings?.confirmation_mode
    ) ?
      validation
    : null;
  const activeValidationKey =
    activeProfile === null ? null : (
      modelValidationKey(activeProfile, actionSettings?.confirmation_mode)
    );
  const modelStatus = useMemo(() => {
    if (modelSettings === null) {
      return {
        kind: "checking" as const,
        message: "Loading model settings.",
      };
    }
    if (activeProfile === null) {
      return {
        kind: "setup" as const,
        message: "Choose a model to begin.",
      };
    }
    if (activeProfile.credential_status === "missing") {
      return {
        kind: "setup" as const,
        message: "Add the credential required by the selected model.",
      };
    }
    if (activeValidation === null) {
      if (validationFailureKey === activeValidationKey) {
        return {
          kind: "denied" as const,
          message: "Access to the selected model could not be verified.",
        };
      }
      return {
        kind: "checking" as const,
        message: "Checking access to the selected model.",
      };
    }
    if (activeValidation.policy_decision.decision !== "allow") {
      return {
        kind: "denied" as const,
        message: "The selected model is not authorized in this environment.",
      };
    }
    return { kind: "ready" as const, message: "" };
  }, [
    activeProfile,
    activeValidation,
    activeValidationKey,
    modelSettings,
    validationFailureKey,
  ]);
  const modelReady = modelStatus.kind === "ready";
  const activeModelLabel =
    modelSettings === null ? "Loading"
    : activeProfile === null ? "Not configured"
    : modelProfileLabel(activeProfile, modelSettings);
  const conversation = useMemo(
    () =>
      mergeConversationMessages(
        localConversation.filter((message) => message.sessionId === sessionId),
        viewModel.conversation,
      ),
    [localConversation, sessionId, viewModel.conversation],
  );
  const pendingActions = viewModel.approvalControls.filter(
    (control) =>
      control.targetType === "tool-call" && control.decision === null,
  );

  useEffect(() => {
    scrollConversationEnd(conversationEndRef.current);
  }, [conversation.length]);

  useEffect(() => {
    if (
      activeProfile === null ||
      activeProfile.credential_status === "missing"
    ) {
      return;
    }
    let active = true;
    const requestKey = modelValidationKey(
      activeProfile,
      actionSettings?.confirmation_mode,
    );
    void resolvedClient
      .validateModelProfile(activeProfile.profile_id)
      .then((result) => {
        if (!active) return;
        setValidation(result);
        setValidationFailureKey((current) =>
          current === requestKey ? null : current,
        );
      })
      .catch((caught: unknown) => {
        if (!active) return;
        setValidation(null);
        setValidationFailureKey(requestKey);
        setError(errorMessage(caught));
      });
    return () => {
      active = false;
    };
  }, [actionSettings?.confirmation_mode, activeProfile, resolvedClient]);

  const send = async (
    kind: Parameters<typeof createCommand>[1],
    payload: Record<string, JsonValue> = {},
  ) => {
    if (sessionId === null) return false;
    setRequestStatus("busy");
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
      await refreshSessions();
      setRequestStatus("idle");
      return true;
    } catch (caught) {
      setError(errorMessage(caught));
      setRequestStatus("error");
      return false;
    }
  };

  const exportAudit = async () => {
    if (sessionId === null || !(await send("audit.export"))) return;
    try {
      const exported = await resolvedClient.getAuditExport(sessionId);
      downloadTextFile(exported.filename, exported.content);
    } catch (caught) {
      setError(errorMessage(caught));
      setRequestStatus("error");
    }
  };

  const submitPrompt = () => {
    const value = prompt.trim();
    if (!value || !modelReady || requestStatus === "busy" || sessionId === null)
      return;
    setPrompt("");
    void send("chat", { prompt: value });
  };

  const createSession = async () => {
    const generation = ++selectionGeneration.current;
    setError(null);
    try {
      const created = await resolvedClient.createSession();
      setSessions((current) => mergeSessionSummaries(current, [created]));
      if (selectionGeneration.current !== generation) return;
      setEvents([]);
      setPrompt("");
      setSessionId(created.session_id);
      setMobileSessionsOpen(false);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const renameSession = async (title: string) => {
    if (sessionId === null) return;
    try {
      const updated = await resolvedClient.renameSession(sessionId, title);
      setSessions((current) => mergeSessionSummaries(current, [updated]));
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const selectSession = (nextSessionId: string) => {
    selectionGeneration.current += 1;
    setEvents([]);
    setPrompt("");
    setSessionId(nextSessionId);
    setMobileSessionsOpen(false);
    setPanel(null);
  };

  const openPanel = (nextPanel: Exclude<UtilityPanel, null>) => {
    utilityTriggerRef.current =
      document.activeElement instanceof HTMLElement ?
        document.activeElement
      : null;
    setPanel(nextPanel);
    setMobileSessionsOpen(false);
  };

  const decideAction = (
    decision: "approve" | "deny",
    control: ApprovalControl,
  ) =>
    send(decision, {
      target_id: control.targetId,
      target_type: "tool-call",
    });

  const refreshSettings = async () => {
    try {
      const [actions, settings, artifacts] = await Promise.all([
        resolvedClient.getActionSettings(),
        resolvedClient.getModelSettings(),
        resolvedClient.getModelArtifacts(),
      ]);
      setActionSettings(actions);
      setModelSettings(settings);
      setModelArtifacts(artifacts);
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const selectActionMode = async (mode: ActionConfirmationMode) => {
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
    try {
      setModelSettings(await resolvedClient.saveModelProfile(profileDraft));
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const connectModel = async (request: ModelConnectRequest) => {
    try {
      const settings = await resolvedClient.connectModel(request);
      setModelSettings(settings);
    } catch (caught) {
      setError(errorMessage(caught));
      throw caught;
    }
  };

  const discoverModels = (request: ModelCatalogRequest) =>
    resolvedClient.discoverModels(request);

  const selectProfile = async (profileId: string) => {
    try {
      setModelSettings(await resolvedClient.selectModelProfile(profileId));
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const validateProfile = async (profileId: string | undefined) => {
    const resolvedProfileId = profileId ?? activeProfile?.profile_id;
    if (resolvedProfileId === undefined) return;
    const profile = modelSettings?.profiles.find(
      (candidate) => candidate.profile_id === resolvedProfileId,
    );
    const requestKey =
      profile === undefined ? null : (
        modelValidationKey(profile, actionSettings?.confirmation_mode)
      );
    try {
      setValidation(
        await resolvedClient.validateModelProfile(resolvedProfileId),
      );
      if (requestKey !== null) {
        setValidationFailureKey((current) =>
          current === requestKey ? null : current,
        );
      }
    } catch (caught) {
      setValidation(null);
      if (requestKey !== null) setValidationFailureKey(requestKey);
      setError(errorMessage(caught));
    }
  };

  const railProps = {
    activePanel: panel,
    selectedSessionId: sessionId,
    sessions,
    onExportAudit: () => void exportAudit(),
    onNewSession: () => void createSession(),
    onOpenPanel: openPanel,
    onSelectSession: selectSession,
  };

  return (
    <SpeziProvider
      router={{
        Link: ({ href, ...props }) => <a href={href ?? "#"} {...props} />,
      }}
    >
      <main className="app-shell">
        <SessionRail {...railProps} />
        <section className="workbench">
          <WorkspaceHeader
            actionSettings={actionSettings}
            context={viewModel.context}
            modelDetail={activeProfile?.model ?? null}
            modelLabel={activeModelLabel}
            modelStatus={modelStatus.kind}
            key={sessionId ?? "loading"}
            requestStatus={requestStatus}
            session={selectedSession}
            onDetect={() => void send("detect")}
            onOpenMenu={() => setMobileSessionsOpen(true)}
            onRename={(title) => void renameSession(title)}
          />

          {error ?
            <div className="error-banner" role="alert">
              <span>{error}</span>
              <Button
                aria-label="Dismiss error"
                size="sm"
                variant="ghost"
                onClick={() => setError(null)}
              >
                <X size={16} />
              </Button>
            </div>
          : null}

          <ConversationWorkspace
            conversation={conversation}
            conversationEndRef={conversationEndRef}
            modelConfigured={modelReady}
            modelMessage={modelStatus.message}
            paused={viewModel.paused}
            pendingActions={pendingActions}
            prompt={prompt}
            requestStatus={requestStatus}
            onDecision={(decision, control) =>
              void decideAction(decision, control)
            }
            onOpenSettings={() => openPanel("settings")}
            onPauseToggle={() =>
              void send(viewModel.paused ? "resume" : "pause")
            }
            onPrompt={setPrompt}
            onSubmit={submitPrompt}
          />
        </section>

        <Sheet open={mobileSessionsOpen} onOpenChange={setMobileSessionsOpen}>
          <SheetContent className="mobile-session-sheet" side="left" size="sm">
            <SheetTitle className="visually-hidden">
              Heartwood sessions
            </SheetTitle>
            <SheetDescription className="visually-hidden">
              Create and switch between analysis sessions.
            </SheetDescription>
            <SessionRailContent {...railProps} />
          </SheetContent>
        </Sheet>

        <UtilitySheet
          actions={actionSettings}
          artifacts={modelArtifacts}
          events={events}
          panel={panel}
          profileDraft={profileDraft}
          settings={modelSettings}
          skillApproved={skillApproved}
          skillCandidate={skillCandidate}
          skillSettings={skillSettings}
          skillSource={skillSource}
          validation={activeValidation}
          onClose={() => setPanel(null)}
          onConnectModel={connectModel}
          onDiscoverModels={discoverModels}
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
          onExportAudit={() => void exportAudit()}
          onInspectSkill={() =>
            void resolvedClient
              .inspectSkill(skillSource.trim())
              .then((summary) => {
                setSkillCandidate(summary);
                setSkillApproved(false);
              })
              .catch((caught: unknown) => setError(errorMessage(caught)))
          }
          onInstallSkill={() =>
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
          onProfileDraft={setProfileDraft}
          onRefreshActivity={() =>
            sessionId === null ? undefined : (
              void resolvedClient
                .replayEvents(sessionId)
                .then(({ events: replayed }) => setEvents(replayed))
                .catch((caught: unknown) => setError(errorMessage(caught)))
            )
          }
          onRefreshSettings={() => void refreshSettings()}
          onRestoreFocus={() => utilityTriggerRef.current?.focus()}
          onRemoveProfile={(profileId) =>
            void resolvedClient
              .removeModelProfile(profileId)
              .then(setModelSettings)
              .catch((caught: unknown) => setError(errorMessage(caught)))
          }
          onRemoveSkill={(name) =>
            void resolvedClient
              .removeSkill(name)
              .then(setSkillSettings)
              .catch((caught: unknown) => setError(errorMessage(caught)))
          }
          onSaveProfile={() => void saveProfile()}
          onSelectActionMode={(mode) => void selectActionMode(mode)}
          onSelectProfile={(profileId) => void selectProfile(profileId)}
          onSetSkillApproved={setSkillApproved}
          onSetSkillSource={setSkillSource}
          onValidateProfile={(profileId) => void validateProfile(profileId)}
        />
      </main>
    </SpeziProvider>
  );
};

const initializeSessions = async (
  client: HeartwoodClient,
  initialSessionId: string | undefined,
): Promise<InitialState> => {
  const listed = (await client.listSessions()).sessions;
  if (initialSessionId !== undefined) {
    const existing = listed.find(
      (session) => session.session_id === initialSessionId,
    );
    const selected = existing ?? (await client.getSession(initialSessionId));
    return {
      selectedSessionId: selected.session_id,
      sessions: mergeSessionSummaries(listed, [selected]),
    };
  }
  if (listed[0]) {
    return { selectedSessionId: listed[0].session_id, sessions: listed };
  }
  const created = await client.createSession();
  return { selectedSessionId: created.session_id, sessions: [created] };
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
  for (const message of [...localMessages, ...eventMessages])
    messages.set(message.id, message);
  return [...messages.values()].sort(
    (left, right) =>
      left.sequence - right.sequence || left.id.localeCompare(right.id),
  );
};

const mergeSessionSummaries = (
  current: SessionSummary[],
  next: SessionSummary[],
): SessionSummary[] => {
  const summaries = new Map(
    current.map((session) => [session.session_id, session]),
  );
  for (const session of next) summaries.set(session.session_id, session);
  return [...summaries.values()].sort(
    (left, right) =>
      right.updated_at.localeCompare(left.updated_at) ||
      right.session_id.localeCompare(left.session_id),
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

const modelValidationKey = (
  profile: ModelProfile,
  confirmationMode: ActionConfirmationMode | undefined,
): string =>
  JSON.stringify([profile.profile_id, profile.model, confirmationMode ?? null]);

const errorMessage = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

const downloadTextFile = (filename: string, content: string): void => {
  if (typeof URL.createObjectURL !== "function") return;
  const url = URL.createObjectURL(
    new Blob([content], { type: "application/x-ndjson" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
};
