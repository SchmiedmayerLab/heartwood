/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type * as Api from "./apiTypes.generated";
import type * as Projection from "./sessionProjection.generated";

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export type CommandKind =
  "approve" | "deny" | "chat" | "pause" | "resume" | "replay" | "audit.export";

export type EventKind =
  | "command.received"
  | "approval.recorded"
  | "policy.decision.recorded"
  | "model_call.decision.recorded"
  | "user_message.recorded"
  | "agent_message.emitted"
  | "tool_call.proposed"
  | "confirmation.requested"
  | "confirmation.resolved"
  | "tool.execution.recorded"
  | "session.paused"
  | "session.resumed"
  | "agent.lifecycle.updated"
  | "task.plan.updated"
  | "model.usage.updated"
  | "subagent.updated"
  | "audit.export.recorded"
  | "error.recorded";

export interface SessionCommand {
  schema_version: "heartwood.session-command.v1";
  command_id: string;
  session_id: string;
  kind: CommandKind;
  actor_id: string;
  created_at: string;
  payload: Record<string, JsonValue>;
}

export interface SessionEvent {
  schema_version: "heartwood.session-event.v1";
  event_id: string;
  session_id: string;
  sequence: number;
  kind: EventKind;
  occurred_at: string;
  payload: Record<string, JsonValue>;
  previous_event_hash: string | null;
}

export type ActionRisk = Api.ActionRisk;
export type ActivityItem = Projection.ProjectionActivity;
export type ConversationMessage = Projection.ProjectionMessage;
export type ProjectionActionRecord = Projection.ProjectionActionRecord;
export type ProjectionApprovalGroup = Projection.ProjectionApprovalGroup;
export type ProjectionModelContext = Projection.ProjectionModelContext;
export type ProjectionLifecycle = Projection.ProjectionLifecycleState;
export type ProjectionLifecycleStatus = ProjectionLifecycle["status"];
export type ProjectionResearcherStatus = Projection.ProjectionResearcherStatus;
export type ProjectionTask = Projection.ProjectionTask;
export type ProjectionUsage = Projection.ProjectionUsage;
export type ProjectionSubagent = Projection.ProjectionSubagent;
export type ProjectionSuggestion = Projection.ProjectionSuggestion;
export type ProjectionCommandOutcome = Projection.ProjectionCommandOutcome;
export type SessionProjection = Projection.SessionProjection;
export type ProjectionCommand = SessionProjection["availableCommands"][number];

export type {
  ActionConfirmationMode,
  ActionConfirmationRequest,
  CredentialKind,
  CredentialStatus,
  CustomLocalModelDownloadRequest,
  InterfaceKind,
  LocalModelImportRequest,
  LocalModelQualification,
  LocalModelRuntime,
  LocalModelTier,
  ModelCatalogRequest,
  ModelConnectRequest,
  ModelDownloadRequest,
  ModelProfileRequest,
  ModelRepositoryRequest,
  ModelSelectionRequest,
  ModelSource,
  ModelSourceRequest,
  SessionCreateRequest,
  SessionRenameRequest,
  SkillInspectRequest,
  SkillInstallRequest,
  SubscriptionDeviceLoginRequest,
  SubscriptionDevicePollRequest,
  ToolCallParser,
} from "./apiTypes.generated";

export type SessionStatus = Api.SessionSummaryResponse["status"];
export type SessionSummary = Api.SessionSummaryResponse;
export type SessionList = Api.SessionListResponse;
export type AuditExport = Api.AuditExportResponse;
export type ActionModeOption = Api.ActionModeOptionResponse;
export type ActionPresentation = Api.ActionPresentationResponse;
export type ActionSettings = Api.ActionSettingsResponse;
export type ReadinessState = Api.ProjectReadinessResponse["state"];
export type ReadinessCheck = Api.ReadinessCheckResponse;
export type ProjectReadiness = Api.ProjectReadinessResponse;
export type PlatformCapabilities = Api.PlatformCapabilitiesResponse;
export type SetupPhase = Api.StartupPlanResponse["phase"];
export type StartupPlan = Api.StartupPlanResponse;
export type ModelProfile = Api.ModelProfileResponse;
export type ModelProfileDraft = Api.ModelProfileRequest;
export type ModelConnectionProtocol = Api.ModelConnectionResponse["protocol"];
export type ModelConnectionSource = Api.ModelConnectionResponse["source"];
export type ModelConnectionGroup = Api.ModelConnectionResponse["group"];
export type ModelConnection = Api.ModelConnectionResponse;
export type ModelCatalogEntry = Api.ModelCatalogEntryResponse;
export type ModelCatalog = Api.ModelCatalogResponse;
export type SubscriptionDeviceLogin = Api.SubscriptionDeviceLoginResponse;
export type CredentialStoreAvailability =
  Api.CredentialStoreAvailabilityResponse;
export type CredentialBindingStatus = Api.CredentialBindingStatusResponse;
export type CredentialSettings = Api.CredentialSettingsResponse;
export type CredentialIsolation = Api.CredentialIsolationResponse;
export type ModelPreset = Api.ModelPresetResponse;
export type ModelSourceOption = Api.ModelSourceOptionResponse;
export type ModelSettings = Api.ModelSettingsResponse;
export type ModelValidation = Api.ModelValidationResponse;
export type ModelArtifact = Api.ModelArtifactResponse;
export type ModelDownload = Api.ModelDownloadResponse;
export type LocalModelChoice = Api.LocalModelChoiceResponse;
export type GpuCapacity = Api.GpuCapacityResponse;
export type ModelRepositoryPlan = Api.ModelRepositoryPlanResponse;
export type LocalModelImportResult = Api.LocalModelImportResponse;
export type ModelSnapshot = Api.ModelSnapshotResponse;
export type ModelArtifacts = Api.ModelArtifactsResponse;
export type SkillSummary = Api.SkillSummaryResponse;
export type SkillSettings = Api.SkillSettingsResponse;
export type WorkspaceTree = Api.WorkspaceTreeResponse;
export type WorkspaceTreeEntry = Api.WorkspaceTreeEntryResponse;
export type WorkspaceFile = Api.WorkspaceFileResponse;
export type WorkspaceChanges = Api.WorkspaceChangesResponse;
export type WorkspaceChange = Api.WorkspaceChangeResponse;
export type WorkspaceDiff = Api.WorkspaceDiffResponse;
