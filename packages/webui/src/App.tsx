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
import {
  requestActivityForCommand,
  type RequestActivity,
} from "./requestActivity";
import type {
  ActionConfirmationMode,
  ActionSettings,
  ApprovalControl,
  ConversationMessage,
  JsonValue,
  LocalModelImportRequest,
  ModelArtifacts,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelProfile,
  ModelSource,
  ModelSettings,
  ModelValidation,
  ProjectReadiness,
  SessionEvent,
  SessionSummary,
  SkillSettings,
  SkillSummary,
  StartupPlan,
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
  selectedSessionId: string | null;
  sessions: SessionSummary[];
}

const emptyProfile = (): ModelProfile => ({
  profile_id: "custom-profile",
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
  const [requestActivity, setRequestActivity] =
    useState<RequestActivity | null>(null);
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
  const [projectReadiness, setProjectReadiness] =
    useState<ProjectReadiness | null>(null);
  const [startupPlan, setStartupPlan] = useState<StartupPlan | null>(null);
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
  const commandInFlight = useRef(false);
  const setupOpened = useRef(false);
  const modelPollingError = useRef<string | null>(null);

  const refreshSessions = useCallback(async () => {
    const response = await resolvedClient.listSessions();
    setSessions(response.sessions);
    return response.sessions;
  }, [resolvedClient]);

  const loadProjectState = useCallback(async () => {
    const [actions, models, artifacts, skills, startup] = await Promise.all([
      resolvedClient.getActionSettings(),
      resolvedClient.getModelSettings(),
      resolvedClient.getModelArtifacts(),
      resolvedClient.getSkillSettings(),
      resolvedClient.getStartupPlan(),
    ]);
    return { actions, models, artifacts, skills, startup };
  }, [resolvedClient]);

  const refreshProjectState = useCallback(async () => {
    const state = await loadProjectState();
    const { actions, models, artifacts, skills, startup } = state;
    setActionSettings(actions);
    setModelSettings(models);
    setModelArtifacts(artifacts);
    setSkillSettings(skills);
    setStartupPlan(startup);
    setProjectReadiness(startup.readiness);
    return { models, readiness: startup.readiness };
  }, [loadProjectState]);

  const refreshReadiness = useCallback(async () => {
    const startup = await resolvedClient.getStartupPlan();
    setStartupPlan(startup);
    setProjectReadiness(startup.readiness);
    return startup.readiness;
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
    let active = true;
    void loadProjectState()
      .then(({ actions, models, artifacts, skills, startup }) => {
        if (!active) return;
        setActionSettings(actions);
        setModelSettings(models);
        setModelArtifacts(artifacts);
        setSkillSettings(skills);
        setStartupPlan(startup);
        setProjectReadiness(startup.readiness);
        if (
          !setupOpened.current &&
          startup.readiness.state === "setup-required" &&
          models.active_profile === null
        ) {
          setupOpened.current = true;
          setPanel("settings");
        }
      })
      .catch((caught: unknown) => {
        if (active) setError(errorMessage(caught));
      });
    return () => {
      active = false;
    };
  }, [loadProjectState]);

  useEffect(() => {
    const refresh = (): void => {
      if (document.visibilityState === "visible") {
        void refreshProjectState().catch((caught: unknown) =>
          setError(errorMessage(caught)),
        );
      }
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [refreshProjectState]);

  const modelDownloadActive =
    modelArtifacts?.downloads.some(
      (download) => download.status === "downloading",
    ) ?? false;

  useEffect(() => {
    if (!modelDownloadActive) {
      const recoveredError = modelPollingError.current;
      modelPollingError.current = null;
      if (recoveredError !== null) {
        setError((current) => (current === recoveredError ? null : current));
      }
      return;
    }
    let active = true;
    let timer: number | null = null;
    const poll = async (): Promise<void> => {
      try {
        const artifacts = await resolvedClient.getModelArtifacts();
        if (!active) return;
        const recoveredError = modelPollingError.current;
        modelPollingError.current = null;
        if (recoveredError !== null) {
          setError((current) => (current === recoveredError ? null : current));
        }
        setModelArtifacts(artifacts);
        if (artifacts.downloads.some((item) => item.status === "downloading")) {
          timer = window.setTimeout(() => void poll(), 500);
        } else {
          void refreshProjectState().catch((caught: unknown) =>
            setError(errorMessage(caught)),
          );
        }
      } catch (caught) {
        if (active) {
          const message = errorMessage(caught);
          modelPollingError.current = message;
          setError(message);
          timer = window.setTimeout(() => void poll(), 2_000);
        }
      }
    };
    timer = window.setTimeout(() => void poll(), 500);
    return () => {
      active = false;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [modelDownloadActive, refreshProjectState, resolvedClient]);

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
    if (modelSettings === null || projectReadiness === null) {
      return {
        kind: "checking" as const,
        message: "Checking project setup.",
      };
    }
    if (projectReadiness.state === "recovery-required") {
      return {
        kind: "denied" as const,
        message: "Resolve the project setup issues shown in Settings.",
      };
    }
    if (projectReadiness.state === "compute-required") {
      return {
        kind: "setup" as const,
        message:
          "Restart with heartwood --interface web to start the selected Heartwood-managed model.",
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
    if (projectReadiness.state === "setup-required") {
      return {
        kind: "setup" as const,
        message: "Complete this project's model setup in Settings.",
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
    projectReadiness,
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
  }, [conversation.length, requestStatus]);

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
    if (sessionId === null || commandInFlight.current) return false;
    commandInFlight.current = true;
    setRequestActivity(requestActivityForCommand(kind));
    setRequestStatus("busy");
    setError(null);
    try {
      const command = createCommand(sessionId, kind, events.length, payload);
      const submittedPrompt = promptContent(payload);
      if (kind === "chat" && submittedPrompt) {
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
    } finally {
      commandInFlight.current = false;
      setRequestActivity(null);
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
    if (nextPanel !== "activity") {
      void refreshProjectState().catch((caught: unknown) =>
        setError(errorMessage(caught)),
      );
    }
  };

  const decideAction = (
    decision: "approve" | "deny",
    control: ApprovalControl,
  ) =>
    send(decision, {
      target_id: control.targetId,
      target_type: "tool-call",
    });

  const selectActionMode = async (mode: ActionConfirmationMode) => {
    try {
      setActionSettings(
        await resolvedClient.selectActionConfirmationMode(mode),
      );
      setValidation(null);
      await refreshReadiness();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const saveProfile = async () => {
    try {
      setModelSettings(await resolvedClient.saveModelProfile(profileDraft));
      await refreshReadiness();
    } catch (caught) {
      setError(errorMessage(caught));
    }
  };

  const connectModel = async (request: ModelConnectRequest) => {
    try {
      const settings = await resolvedClient.connectModel(request);
      setModelSettings(settings);
      await refreshReadiness();
    } catch (caught) {
      setError(errorMessage(caught));
      throw caught;
    }
  };

  const discoverModels = (request: ModelCatalogRequest) =>
    resolvedClient.discoverModels(request);

  const configureModelSource = async (sourceId: ModelSource) => {
    try {
      const settings = await resolvedClient.configureModelSource(sourceId);
      setModelSettings(settings);
      await refreshReadiness();
      return settings;
    } catch (caught) {
      setError(errorMessage(caught));
      throw caught;
    }
  };

  const forgetCredential = async (connectionId: string) => {
    try {
      await resolvedClient.forgetCredential(connectionId);
      await refreshProjectState();
    } catch (caught) {
      setError(errorMessage(caught));
      throw caught;
    }
  };

  const selectProfile = async (profileId: string) => {
    try {
      setModelSettings(await resolvedClient.selectModelProfile(profileId));
      await refreshReadiness();
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
            modelDetail={activeProfile?.model ?? null}
            modelLabel={activeModelLabel}
            modelStatus={modelStatus.kind}
            platformLabel={
              startupPlan?.capabilities.display_name ?? "Checking environment"
            }
            projectLabel={projectLabel(startupPlan?.project_root)}
            key={sessionId ?? "loading"}
            requestStatus={requestStatus}
            session={selectedSession}
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
            requestActivity={requestActivity}
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
          projectReadiness={projectReadiness}
          startupPlan={startupPlan}
          settings={modelSettings}
          skillApproved={skillApproved}
          skillCandidate={skillCandidate}
          skillSettings={skillSettings}
          skillSource={skillSource}
          validation={activeValidation}
          onClose={() => setPanel(null)}
          onConnectModel={connectModel}
          onConfigureModelSource={configureModelSource}
          onDiscoverModels={discoverModels}
          onForgetCredential={forgetCredential}
          onDownload={(modelId) =>
            void resolvedClient
              .downloadLocalModel(modelId)
              .then((download) =>
                setModelArtifacts((current) =>
                  current === null ? current : (
                    {
                      ...current,
                      downloads: [
                        ...current.downloads.filter(
                          (item) => item.model_id !== modelId,
                        ),
                        download,
                      ],
                    }
                  ),
                ),
              )
              .catch((caught: unknown) => setError(errorMessage(caught)))
          }
          onDownloadCustom={async (request) => {
            await resolvedClient.downloadCustomLocalModel(request);
            setModelArtifacts(await resolvedClient.getModelArtifacts());
          }}
          onExportAudit={() => void exportAudit()}
          onInspectModelRepository={(request) =>
            resolvedClient.inspectModelRepository(request)
          }
          onImportLocalModel={async (request: LocalModelImportRequest) => {
            await resolvedClient.importLocalModel(request);
            const [models, artifacts, startup] = await Promise.all([
              resolvedClient.getModelSettings(),
              resolvedClient.getModelArtifacts(),
              resolvedClient.getStartupPlan(),
            ]);
            setModelSettings(models);
            setModelArtifacts(artifacts);
            setStartupPlan(startup);
            setProjectReadiness(startup.readiness);
          }}
          onInitializeProject={async () => {
            const startup = await resolvedClient.initializeProject();
            setStartupPlan(startup);
            setProjectReadiness(startup.readiness);
            if (sessions.length === 0) {
              const created = await resolvedClient.ensureDefaultSession();
              setSessions([created]);
              setSessionId(created.session_id);
            }
          }}
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
          onRefreshSettings={() =>
            void refreshProjectState().catch((caught: unknown) =>
              setError(errorMessage(caught)),
            )
          }
          onRestoreFocus={() => utilityTriggerRef.current?.focus()}
          onRemoveProfile={(profileId) =>
            void resolvedClient
              .removeModelProfile(profileId)
              .then((settings) => {
                setModelSettings(settings);
                return refreshReadiness();
              })
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
  const startup = await client.getStartupPlan();
  if (startup.phase === "project-review") {
    return { selectedSessionId: null, sessions: [] };
  }
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
  const created = await client.ensureDefaultSession();
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

const projectLabel = (root: string | undefined): string => {
  if (root === undefined) return "Checking project";
  const parts = root.split(/[\\/]/u).filter(Boolean);
  return parts.at(-1) ?? root;
};

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
