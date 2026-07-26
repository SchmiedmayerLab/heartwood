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
import { selectedActionMode } from "./actionPresentation";
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
  JsonValue,
  LocalModelImportRequest,
  ModelArtifacts,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelProfile,
  ModelProfileDraft,
  ModelSource,
  ModelSettings,
  ModelValidation,
  ProjectReadiness,
  ProjectionApprovalGroup,
  SessionCommand,
  SessionProjection,
  SessionSummary,
  SkillSettings,
  SkillSummary,
  StartupPlan,
} from "./types";

interface AppProps {
  client?: HeartwoodClient;
  initialSessionId?: string;
}

interface InitialState {
  selectedSessionId: string | null;
  sessions: SessionSummary[];
}

const emptyProfile = (): ModelProfileDraft => ({
  profile_id: "custom-profile",
  model: "openai/",
  policy_endpoint: "http://127.0.0.1:8765/v1/chat/completions",
  capability_tier: "supervised",
  base_url: "http://127.0.0.1:8765/v1",
  credential_kind: "none",
  auth_type: "api_key",
  subscription_vendor: null,
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
  const [projection, setProjection] = useState<SessionProjection | null>(null);
  const [prompt, setPrompt] = useState("");
  const [requestStatus, setRequestStatus] = useState<"idle" | "busy" | "error">(
    "idle",
  );
  const [requestActivity, setRequestActivity] =
    useState<RequestActivity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryCommand, setRetryCommand] = useState<SessionCommand | null>(null);
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
  const [profileDraft, setProfileDraft] =
    useState<ModelProfileDraft>(emptyProfile);
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
  const retiredStreamEpochs = useRef(new Map<string, Set<string>>());
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

  const startSubscriptionLogin = useCallback(
    (connectionId: string) =>
      resolvedClient.startSubscriptionDeviceLogin(connectionId),
    [resolvedClient],
  );

  const pollSubscriptionLogin = useCallback(
    (connectionId: string, loginId: string) =>
      resolvedClient.pollSubscriptionDeviceLogin(connectionId, loginId),
    [resolvedClient],
  );

  const refreshSettings = useCallback(() => {
    void refreshProjectState().catch((caught: unknown) =>
      setError(errorMessage(caught)),
    );
  }, [refreshProjectState]);

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
    let closeStream = (): void => undefined;
    void resolvedClient
      .replayEvents(sessionId)
      .then(({ projection: replayed }) => {
        if (!active) return;
        setProjection((current) =>
          selectProjection(
            current,
            replayed,
            sessionId,
            retiredStreamEpochs.current,
          ),
        );
        setRequestStatus("idle");
        closeStream = resolvedClient.streamSession(
          sessionId,
          replayed.revision,
          (streamed) => {
            if (!active) return;
            setProjection((current) =>
              selectProjection(
                current,
                streamed,
                sessionId,
                retiredStreamEpochs.current,
              ),
            );
            refreshTimer ??= window.setTimeout(() => {
              refreshTimer = null;
              void refreshSessions().catch((caught: unknown) =>
                setError(errorMessage(caught)),
              );
            }, 250);
          },
          (streamError) => {
            if (!active) return;
            setError(`Live session updates stopped: ${streamError.message}`);
            setRequestStatus("error");
          },
        );
      })
      .catch((caught: unknown) => {
        if (active) {
          setError(errorMessage(caught));
          setRequestStatus("error");
        }
      });
    return () => {
      active = false;
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      closeStream();
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
  const conversation = projection?.conversation ?? [];
  const activeActionMode = selectedActionMode(actionSettings);
  const actionModeLockedReason =
    requestStatus === "busy" ?
      "Wait for the active request to finish before changing this setting."
    : (
      projection?.pendingApproval !== null &&
      projection?.pendingApproval !== undefined
    ) ?
      "Resolve the pending action set before changing this setting."
    : projection?.lifecycle.status === "running" ?
      "Wait for the active task to reach a review point before changing this setting."
    : actionSettings?.change_allowed === false ?
      actionSettings.change_blocked_reason
    : null;

  useEffect(() => {
    scrollConversationEnd(conversationEndRef.current);
  }, [conversation.length, projection?.streamingText, requestStatus]);

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
    existingCommand?: SessionCommand,
  ) => {
    if (sessionId === null || commandInFlight.current) return false;
    commandInFlight.current = true;
    const command = existingCommand ?? createCommand(sessionId, kind, payload);
    setRequestActivity(requestActivityForCommand(kind));
    setRequestStatus("busy");
    setError(null);
    try {
      const response = await resolvedClient.postCommand(command);
      setProjection((current) =>
        selectProjection(
          current,
          response.projection,
          sessionId,
          retiredStreamEpochs.current,
        ),
      );
      await refreshSessions();
      const outcome = response.projection.lastCommandOutcome;
      if (
        outcome?.commandId === command.command_id &&
        outcome.status === "rejected"
      ) {
        setError(outcome.message ?? "The command was rejected.");
        setRequestStatus("error");
        return false;
      }
      setRetryCommand(null);
      setRequestStatus("idle");
      return true;
    } catch (caught) {
      setRetryCommand(command);
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
    if (
      !value ||
      !modelReady ||
      !projection?.availableCommands.includes("chat") ||
      requestStatus === "busy" ||
      sessionId === null
    )
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
      setProjection(null);
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
    setProjection(null);
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
    approval: ProjectionApprovalGroup,
  ) =>
    send(decision, {
      target_id: approval.groupId,
      target_type: "action-set",
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
            actionModeLabel={activeActionMode?.label ?? "Loading"}
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
            onOpenActionReview={() => openPanel("action-review")}
            onOpenMenu={() => setMobileSessionsOpen(true)}
            onRename={(title) => void renameSession(title)}
          />

          {error ?
            <div className="error-banner" role="alert">
              <span>{error}</span>
              {retryCommand?.session_id === sessionId ?
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() =>
                    void send(
                      retryCommand.kind,
                      retryCommand.payload,
                      retryCommand,
                    )
                  }
                >
                  Retry request
                </Button>
              : null}
              <Button
                aria-label="Dismiss error"
                size="sm"
                variant="ghost"
                onClick={() => {
                  setError(null);
                  setRetryCommand(null);
                }}
              >
                <X size={16} />
              </Button>
            </div>
          : null}

          <ConversationWorkspace
            actionModeLabel={activeActionMode?.label ?? null}
            actionPresentation={actionSettings?.presentation ?? null}
            conversationEndRef={conversationEndRef}
            modelConfigured={modelReady}
            modelMessage={modelStatus.message}
            projection={projection}
            prompt={prompt}
            requestActivity={requestActivity}
            requestStatus={requestStatus}
            onDecision={(decision, approval) =>
              void decideAction(decision, approval)
            }
            onOpenSettings={() => openPanel("settings")}
            onPauseToggle={() => {
              if (projection?.lifecycle.canResume) {
                void send("resume");
              } else if (projection?.lifecycle.canPause) {
                void send("pause");
              }
            }}
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
          actionModeLockedReason={actionModeLockedReason}
          actions={actionSettings}
          artifacts={modelArtifacts}
          panel={panel}
          profileDraft={profileDraft}
          projection={projection}
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
          onPollSubscriptionLogin={pollSubscriptionLogin}
          onStartSubscriptionLogin={startSubscriptionLogin}
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
                .then(({ projection: replayed }) =>
                  setProjection((current) =>
                    selectProjection(
                      current,
                      replayed,
                      sessionId,
                      retiredStreamEpochs.current,
                    ),
                  ),
                )
                .catch((caught: unknown) => setError(errorMessage(caught)))
            )
          }
          onRefreshSettings={refreshSettings}
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
          onSelectActionMode={selectActionMode}
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

const selectProjection = (
  current: SessionProjection | null,
  next: SessionProjection,
  sessionId: string,
  retiredEpochsBySession: Map<string, Set<string>>,
): SessionProjection | null => {
  if (next.sessionId !== sessionId) return current;
  if (current?.sessionId !== sessionId) return next;
  if (next.streamEpoch !== current.streamEpoch) {
    const retiredEpochs =
      retiredEpochsBySession.get(sessionId) ?? new Set<string>();
    if (retiredEpochs.has(next.streamEpoch)) return current;
    retiredEpochs.add(current.streamEpoch);
    retiredEpochsBySession.set(sessionId, retiredEpochs);
    return next;
  }
  if (
    next.revision > current.revision ||
    (next.revision === current.revision &&
      next.streamRevision > current.streamRevision)
  ) {
    return next;
  }
  return current;
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
