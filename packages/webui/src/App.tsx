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
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@stanfordspezi/spezi-web-design-system/components/Tabs";
import { SpeziProvider } from "@stanfordspezi/spezi-web-design-system/SpeziProvider";
import { FileCode2, GitCompareArrows, MessagesSquare, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { selectedActionMode } from "./actionPresentation";
import {
  GatewayClient,
  createCommand,
  type HeartwoodClient,
  type SessionStreamState,
} from "./client";
import { ConversationWorkspace } from "./components/ConversationWorkspace";
import { ProjectWorkspace } from "./components/ProjectWorkspace";
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
  SpecialistSettings,
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

interface ProjectionSelection {
  projection: SessionProjection | null;
  retiredEpoch: string | null;
}

type WorkspaceView = "changes" | "conversation" | "files";
type SessionConnectionState = "replaying" | SessionStreamState;

const connectionPresentation = {
  replaying: {
    label: "Recovering Session",
    detail: "Restoring the saved conversation.",
  },
  connecting: {
    label: "Connecting",
    detail: "Connecting to live session updates.",
  },
  reconnecting: {
    label: "Reconnecting",
    detail: "Live updates were interrupted. Heartwood is reconnecting.",
  },
  degraded: {
    label: "Live Updates Unavailable",
    detail: "The saved conversation remains available.",
  },
} satisfies Record<
  Exclude<SessionConnectionState, "connected">,
  { label: string; detail: string }
>;

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
  const sessionIdRef = useRef<string | null>(null);
  const [projection, setProjection] = useState<SessionProjection | null>(null);
  const projectionRef = useRef<SessionProjection | null>(null);
  const [prompt, setPrompt] = useState("");
  const [requestStatus, setRequestStatus] = useState<"idle" | "busy" | "error">(
    "idle",
  );
  const [requestActivity, setRequestActivity] =
    useState<RequestActivity | null>(null);
  const [connectionState, setConnectionState] =
    useState<SessionConnectionState>("connected");
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [connectionRetry, setConnectionRetry] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [retryCommand, setRetryCommand] = useState<SessionCommand | null>(null);
  const [panel, setPanel] = useState<UtilityPanel>(null);
  const [mobileSessionsOpen, setMobileSessionsOpen] = useState(false);
  const [workspaceView, setWorkspaceView] =
    useState<WorkspaceView>("conversation");
  const [visitedWorkspaceViews, setVisitedWorkspaceViews] = useState<
    ReadonlySet<WorkspaceView>
  >(() => new Set(["conversation"]));
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
  const [specialistSettings, setSpecialistSettings] =
    useState<SpecialistSettings | null>(null);
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

  const acceptProjection = useCallback(
    (next: SessionProjection, selectedSessionId: string) => {
      if (sessionIdRef.current !== selectedSessionId) return;
      const current = projectionRef.current;
      const retiredEpochs =
        retiredStreamEpochs.current.get(selectedSessionId) ?? new Set<string>();
      const selection = selectProjection(
        current,
        next,
        selectedSessionId,
        retiredEpochs,
      );
      if (selection.retiredEpoch !== null) {
        const updatedEpochs = new Set(retiredEpochs);
        updatedEpochs.add(selection.retiredEpoch);
        const updatedBySession = new Map(retiredStreamEpochs.current);
        updatedBySession.set(selectedSessionId, updatedEpochs);
        retiredStreamEpochs.current = updatedBySession;
      }
      if (selection.projection === current) return;
      projectionRef.current = selection.projection;
      setProjection(selection.projection);
    },
    [],
  );

  const updateSessionId = useCallback((nextSessionId: string | null) => {
    if (sessionIdRef.current !== nextSessionId) {
      setConnectionState(nextSessionId === null ? "connected" : "replaying");
      setConnectionError(null);
    }
    sessionIdRef.current = nextSessionId;
    setSessionId(nextSessionId);
  }, []);

  const clearProjection = useCallback(() => {
    projectionRef.current = null;
    setProjection(null);
  }, []);

  const refreshSessions = useCallback(async () => {
    const response = await resolvedClient.listSessions();
    setSessions(response.sessions);
    return response.sessions;
  }, [resolvedClient]);

  const loadProjectState = useCallback(async () => {
    const [actions, models, artifacts, skills, specialists, startup] =
      await Promise.all([
        resolvedClient.getActionSettings(),
        resolvedClient.getModelSettings(),
        resolvedClient.getModelArtifacts(),
        resolvedClient.getSkillSettings(),
        resolvedClient.getSpecialistSettings(),
        resolvedClient.getStartupPlan(),
      ]);
    return { actions, models, artifacts, skills, specialists, startup };
  }, [resolvedClient]);

  const refreshProjectState = useCallback(async () => {
    const state = await loadProjectState();
    const { actions, models, artifacts, skills, specialists, startup } = state;
    setActionSettings(actions);
    setModelSettings(models);
    setModelArtifacts(artifacts);
    setSkillSettings(skills);
    setSpecialistSettings(specialists);
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
        updateSessionId(state.selectedSessionId);
      })
      .catch((caught: unknown) => {
        if (!active || selectionGeneration.current !== generation) return;
        setError(errorMessage(caught));
        setRequestStatus("error");
      });
    return () => {
      active = false;
    };
  }, [initialSessionId, resolvedClient, updateSessionId]);

  useEffect(() => {
    if (sessionId === null) return;
    let active = true;
    let refreshTimer: number | null = null;
    let closeStream = (): void => undefined;
    void resolvedClient
      .replayEvents(sessionId)
      .then(({ projection: replayed }) => {
        if (!active || sessionIdRef.current !== sessionId) return;
        setConnectionError(null);
        acceptProjection(replayed, sessionId);
        if (!commandInFlight.current) {
          setRequestStatus("idle");
        }
        closeStream = resolvedClient.streamSession(
          sessionId,
          replayed.revision,
          {
            onProjection: (streamed) => {
              if (!active || sessionIdRef.current !== sessionId) return;
              acceptProjection(streamed, sessionId);
              refreshTimer ??= window.setTimeout(() => {
                refreshTimer = null;
                void refreshSessions().catch((caught: unknown) =>
                  setError(errorMessage(caught)),
                );
              }, 250);
            },
            onState: (state) => {
              if (!active || sessionIdRef.current !== sessionId) return;
              setConnectionState(state);
              if (state === "connected") setConnectionError(null);
            },
            onError: (streamError) => {
              if (!active || sessionIdRef.current !== sessionId) return;
              setConnectionError(streamError.message);
            },
          },
        );
      })
      .catch((caught: unknown) => {
        if (active && sessionIdRef.current === sessionId) {
          setConnectionState("degraded");
          setConnectionError(errorMessage(caught));
        }
      });
    return () => {
      active = false;
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      closeStream();
    };
  }, [
    acceptProjection,
    connectionRetry,
    refreshSessions,
    resolvedClient,
    sessionId,
  ]);

  useEffect(() => {
    let active = true;
    void loadProjectState()
      .then(({ actions, models, artifacts, skills, specialists, startup }) => {
        if (!active) return;
        setActionSettings(actions);
        setModelSettings(models);
        setModelArtifacts(artifacts);
        setSkillSettings(skills);
        setSpecialistSettings(specialists);
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
    const selectedSessionId = sessionId;
    const selectedGeneration = selectionGeneration.current;
    commandInFlight.current = true;
    const command =
      existingCommand ?? createCommand(selectedSessionId, kind, payload);
    setRequestActivity(requestActivityForCommand(kind));
    setRequestStatus("busy");
    setError(null);
    try {
      const response = await resolvedClient.postCommand(command);
      const selectionIsCurrent = () =>
        sessionIdRef.current === selectedSessionId &&
        selectionGeneration.current === selectedGeneration;
      if (selectionIsCurrent()) {
        acceptProjection(response.projection, selectedSessionId);
      }
      await refreshSessions();
      if (!selectionIsCurrent()) return false;
      const outcome = response.projection.lastCommandOutcome;
      if (
        outcome?.commandId === command.command_id &&
        outcome.status === "rejected"
      ) {
        setRetryCommand(null);
        setRequestStatus("idle");
        return false;
      }
      setRetryCommand(null);
      setRequestStatus("idle");
      return true;
    } catch (caught) {
      if (
        sessionIdRef.current !== selectedSessionId ||
        selectionGeneration.current !== selectedGeneration
      ) {
        return false;
      }
      setRetryCommand(command);
      setError(errorMessage(caught));
      setRequestStatus("error");
      return false;
    } finally {
      commandInFlight.current = false;
      setRequestActivity(null);
      if (
        sessionIdRef.current !== selectedSessionId ||
        selectionGeneration.current !== selectedGeneration
      ) {
        setRequestStatus("idle");
      }
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
      clearProjection();
      setPrompt("");
      setRetryCommand(null);
      setConnectionState("replaying");
      setConnectionError(null);
      updateSessionId(created.session_id);
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
    if (nextSessionId === sessionIdRef.current) return;
    selectionGeneration.current += 1;
    clearProjection();
    setPrompt("");
    setError(null);
    setRetryCommand(null);
    setConnectionState("replaying");
    setConnectionError(null);
    updateSessionId(nextSessionId);
    setMobileSessionsOpen(false);
    setPanel(null);
    setWorkspaceView("conversation");
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
            researcherStatus={projection?.researcherStatus ?? null}
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

          {sessionId !== null && connectionState !== "connected" ?
            <SessionConnectionNotice
              detail={connectionError}
              state={connectionState}
              onRetry={() => {
                setConnectionState("replaying");
                setConnectionError(null);
                setConnectionRetry((current) => current + 1);
              }}
            />
          : null}

          <Tabs
            className="workbench-content"
            value={workspaceView}
            onValueChange={(value) => {
              const nextView = value as WorkspaceView;
              setWorkspaceView(nextView);
              setVisitedWorkspaceViews(
                (current) => new Set([...current, nextView]),
              );
            }}
          >
            <TabsList aria-label="Project view" className="workspace-tabs">
              <TabsTrigger value="conversation">
                <MessagesSquare aria-hidden="true" size={15} />
                Conversation
              </TabsTrigger>
              <TabsTrigger value="files">
                <FileCode2 aria-hidden="true" size={15} />
                Files
              </TabsTrigger>
              <TabsTrigger value="changes">
                <GitCompareArrows aria-hidden="true" size={15} />
                Changes
              </TabsTrigger>
            </TabsList>
            <TabsContent className="workspace-tab-panel" value="conversation">
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
            </TabsContent>
            <TabsContent
              className="workspace-tab-panel"
              forceMount
              value="files"
            >
              {!visitedWorkspaceViews.has("files") ?
                null
              : sessionId === null ?
                <div className="workspace-state" role="status">
                  Select a session to inspect project files.
                </div>
              : <ProjectWorkspace
                  client={resolvedClient}
                  key={`${sessionId}-files`}
                  mode="files"
                  revision={projection?.workspaceRevision ?? -1}
                  sessionId={sessionId}
                />
              }
            </TabsContent>
            <TabsContent
              className="workspace-tab-panel"
              forceMount
              value="changes"
            >
              {!visitedWorkspaceViews.has("changes") ?
                null
              : sessionId === null ?
                <div className="workspace-state" role="status">
                  Select a session to inspect project changes.
                </div>
              : <ProjectWorkspace
                  client={resolvedClient}
                  key={`${sessionId}-changes`}
                  mode="changes"
                  revision={projection?.workspaceRevision ?? -1}
                  sessionId={sessionId}
                />
              }
            </TabsContent>
          </Tabs>
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
          specialistSettings={specialistSettings}
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
              updateSessionId(created.session_id);
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
          onRefreshActivity={() => {
            const selectedSessionId = sessionId;
            const selectedGeneration = selectionGeneration.current;
            if (selectedSessionId === null) return;
            void resolvedClient
              .replayEvents(selectedSessionId)
              .then(({ projection: replayed }) => {
                if (
                  sessionIdRef.current !== selectedSessionId ||
                  selectionGeneration.current !== selectedGeneration
                )
                  return;
                acceptProjection(replayed, selectedSessionId);
              })
              .catch((caught: unknown) => {
                if (
                  sessionIdRef.current === selectedSessionId &&
                  selectionGeneration.current === selectedGeneration
                ) {
                  setError(errorMessage(caught));
                }
              });
          }}
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

const SessionConnectionNotice = ({
  detail,
  state,
  onRetry,
}: {
  detail: string | null;
  state: Exclude<SessionConnectionState, "connected">;
  onRetry: () => void;
}) => {
  const presentation = connectionPresentation[state];
  return (
    <div
      aria-live="polite"
      className={`connection-notice ${state}`}
      role="status"
    >
      <div className="connection-notice-copy">
        <strong>{presentation.label}</strong>
        <span>{detail ?? presentation.detail}</span>
      </div>
      {state === "degraded" ?
        <Button size="sm" variant="outline" onClick={onRetry}>
          Reconnect
        </Button>
      : null}
    </div>
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
  retiredEpochs: ReadonlySet<string>,
): ProjectionSelection => {
  if (next.sessionId !== sessionId) {
    return { projection: current, retiredEpoch: null };
  }
  if (current?.sessionId !== sessionId) {
    return { projection: next, retiredEpoch: null };
  }
  if (next.streamEpoch !== current.streamEpoch) {
    if (retiredEpochs.has(next.streamEpoch)) {
      return { projection: current, retiredEpoch: null };
    }
    return { projection: next, retiredEpoch: current.streamEpoch };
  }
  if (
    next.revision > current.revision ||
    (next.revision === current.revision &&
      next.streamRevision > current.streamRevision)
  ) {
    return { projection: next, retiredEpoch: null };
  }
  return { projection: current, retiredEpoch: null };
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
