/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

/* eslint-disable */
/**
 * Generated from the gateway-owned Pydantic session projection.
 * Run `npm run contracts:generate` after changing a shared request or response.
 */

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | {
      [k: string]: JsonValue;
    };
export type ProjectionActionDetails =
  | ProjectionTerminalActionDetails
  | ProjectionFileEditorActionDetails
  | ProjectionTaskActionDetails
  | ProjectionOtherActionDetails;
/**
 * Events emitted by a Heartwood session.
 *
 * The stream translates OpenHands conversation events so every surface renders
 * the same turns. ``MODEL_CALL_DECISION_RECORDED`` records route authorization
 * before task submission or a continuation that may call the model;
 * ``POLICY_DECISION_RECORDED`` is reserved for other policy decisions.
 */
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
  | "agent.lifecycle.updated"
  | "task.plan.updated"
  | "model.usage.updated"
  | "subagent.updated"
  | "session.paused"
  | "session.resumed"
  | "audit.export.recorded"
  | "error.recorded"
  | "workflow.updated"
  | "workflow.execution.recorded";
export type Reference = string;
export type ExperimentPath = string;
export type Digest = string;
export type ExperimentStatus =
  "started" | "interrupted" | "resumed" | "succeeded" | "failed" | "cancelled";
/**
 * Commands accepted by a Heartwood session.
 */
export type CommandKind =
  | "approve"
  | "deny"
  | "chat"
  | "pause"
  | "resume"
  | "replay"
  | "audit.export"
  | "workflow";
export type WorkflowIdentifier = string;
export type ReviewCategory = "coding" | "statistical" | "reproducibility";
export type ReviewSeverity = "low" | "medium" | "high" | "critical";
export type ResearchText = string;
export type WorkflowInputValue = string;
export type ReviewVerification =
  "verified" | "rejected" | "unsupported" | "stale" | "unavailable";
export type WorkflowText = string;

/**
 * Complete session projection owned by the gateway.
 */
export interface SessionProjection {
  actions: ProjectionActionRecord[];
  activity: ProjectionActivity[];
  availableCommands: ("chat" | "pause" | "resume" | "approve" | "deny")[];
  context: ProjectionModelContext;
  conversation: ProjectionMessage[];
  eventCount: number;
  experiments: ExperimentRun[];
  lastCommandOutcome: ProjectionCommandOutcome | null;
  lifecycle: ProjectionLifecycleState;
  /**
   * Return whether the projected session is paused.
   */
  paused: boolean;
  pendingApproval: ProjectionApprovalGroup | null;
  researcherNotice: ProjectionResearcherNotice | null;
  researcherStatus: ProjectionResearcherStatus;
  revision: number;
  schema_version: "heartwood.session-projection.v1";
  sessionId: string;
  streamEpoch: string;
  streamRevision: number;
  streamingText: string;
  subagents: ProjectionSubagent[];
  suggestions: ProjectionSuggestion[];
  taskPlan: ProjectionTask[];
  usage: ProjectionUsage | null;
  usageByPurpose: ProjectionUsage[];
  workflow: WorkflowRun | null;
  workflowControls: WorkflowControl[];
  workspaceRevision: number;
}
/**
 * One versioned action record correlated across the OpenHands lifecycle.
 */
export interface ProjectionActionRecord {
  actionId: string | null;
  affectedPaths: ProjectionAffectedPath[];
  arguments: {
    [k: string]: JsonValue;
  };
  decision: ("approved" | "rejected") | null;
  details: ProjectionActionDetails;
  groupId: string | null;
  outcome: ProjectionActionOutcome | null;
  proposedSequence: number;
  risk: "high" | "low" | "medium" | "unknown";
  schema_version: "heartwood.action-record.v1";
  state:
    | "proposed"
    | "awaiting-review"
    | "approved"
    | "rejected"
    | "running"
    | "succeeded"
    | "failed"
    | "outcome-unknown";
  summary: string;
  toolCallId: string;
  toolName: string;
  updatedSequence: number;
}
/**
 * Project-relative path attributed to a typed mutating action.
 */
export interface ProjectionAffectedPath {
  effect: "created" | "modified" | "deleted" | "unknown";
  path: string;
  provenance: "file-editor-action";
}
/**
 * Typed terminal arguments from one OpenHands action.
 */
export interface ProjectionTerminalActionDetails {
  command: string;
  isInput: boolean;
  kind: "terminal";
  reset: boolean;
  timeout: number | null;
}
/**
 * Typed file-editor arguments from one OpenHands action.
 */
export interface ProjectionFileEditorActionDetails {
  kind: "file-editor";
  operation:
    "view" | "create" | "str_replace" | "insert" | "undo_edit" | "unknown";
  path: string | null;
}
/**
 * Typed sequential-specialist arguments from one OpenHands action.
 */
export interface ProjectionTaskActionDetails {
  capability: ("advisory" | "project-actions") | null;
  description: string | null;
  kind: "task";
  prompt: string | null;
  resume: string | null;
  roleLabel: string | null;
  subagentType: string | null;
}
/**
 * Typed fallback for an OpenHands tool without a specialized renderer.
 */
export interface ProjectionOtherActionDetails {
  kind: "other";
}
/**
 * Bounded private result of an executed action.
 */
export interface ProjectionActionOutcome {
  exitCode: number;
  result: string | null;
  resultTruncated: boolean;
  summary: string;
}
export interface ProjectionActivity {
  detail: string;
  kind: EventKind;
  label: string;
  sequence: number;
}
export interface ProjectionModelContext {
  modelDecision: string | null;
  modelEndpoint: string | null;
  modelReason: string | null;
}
export interface ProjectionMessage {
  content: string;
  detail: string | null;
  id: string;
  label: string;
  role: "user" | "agent" | "trace";
  sequence: number;
  technicalDetail: string | null;
}
/**
 * One shared projection derived from the scientific execution journal.
 */
export interface ExperimentRun {
  attempt: number;
  definition: ExperimentDefinition;
  evidence: ExperimentEvidence[];
  exit_code: number | null;
  outputs: ExperimentFile[];
  run_id: string;
  schema_version: "heartwood.experiment-run.v1";
  started_at: string;
  status: ExperimentStatus;
  updated_at: string;
}
/**
 * Immutable execution inputs; retries cannot silently change an experiment.
 */
export interface ExperimentDefinition {
  actor_ref: Reference;
  /**
   * @maxItems 256
   */
  code: ExperimentFile[];
  /**
   * @maxItems 256
   */
  code_output_paths: ExperimentPath[];
  entry_point: ExperimentPath | null;
  environment: ExperimentEnvironment;
  git_dirty: boolean | null;
  git_revision: string | null;
  /**
   * @maxItems 256
   */
  inputs: ExperimentFile[];
  invocation_sha256: Digest;
  /**
   * @maxItems 256
   */
  output_paths: ExperimentPath[];
  parameters_sha256: Digest;
  source: "python" | "shell" | "heartwood";
  stage: ExperimentStage | null;
}
/**
 * A declared project file observed at a boundary, without its contents.
 */
export interface ExperimentFile {
  path: ExperimentPath;
  sha256: Digest;
  size_bytes: number;
}
/**
 * Fingerprint of an environment description, not an environment attestation.
 */
export interface ExperimentEnvironment {
  kind: "python" | "container" | "declared";
  sha256: Digest;
  source: "observed" | "declared";
}
/**
 * Association with the owning Heartwood session and research stage.
 */
export interface ExperimentStage {
  session_id: Reference;
  stage_id: Reference;
  tool_call_id: Reference | null;
  workflow_run_id: Reference;
  workflow_sha256: Digest;
}
/**
 * A link to an existing session event, never a copy of its command or result.
 */
export interface ExperimentEvidence {
  event_id: string;
  event_sha256: Digest;
  kind: Reference;
}
/**
 * Gateway-owned outcome of the most recently accepted command.
 */
export interface ProjectionCommandOutcome {
  commandId: string;
  commandKind: CommandKind;
  errorCode: string | null;
  message: string | null;
  status: "accepted" | "rejected";
}
export interface ProjectionLifecycleState {
  canPause: boolean;
  canResume: boolean;
  canSteer: boolean;
  /**
   * User-visible state of the OpenHands conversation.
   */
  status:
    | "idle"
    | "running"
    | "paused"
    | "waiting-for-confirmation"
    | "finished"
    | "error";
}
/**
 * One decision that applies to every listed OpenHands action.
 */
export interface ProjectionApprovalGroup {
  actions: ProjectionActionRecord[];
  decision: ("approved" | "denied") | null;
  decisionScope: "all";
  groupId: string;
}
/**
 * A non-lifecycle outcome that every interface must present.
 */
export interface ProjectionResearcherNotice {
  code: "request-not-applied";
  detail: string;
  label: string;
  noticeId: string;
  tone: "attention" | "danger";
}
/**
 * Stable researcher-facing state derived from the session lifecycle.
 */
export interface ProjectionResearcherStatus {
  code:
    | "ready"
    | "working"
    | "waiting-for-review"
    | "paused"
    | "complete"
    | "denied"
    | "recoverable-failure"
    | "terminal-failure";
  detail: string;
  label: string;
  recoverable: boolean;
  tone: "neutral" | "progress" | "attention" | "success" | "danger";
}
export interface ProjectionSubagent {
  agentName: string;
  invocationId: string;
  parentActionId: string;
  parentSessionId: string;
  resultSummary: string | null;
  reviewProposals: ReviewProposals | null;
  roleLabel: string;
  status: "proposed" | "running" | "completed" | "error" | "rejected";
  statusLabel: string;
  taskId: string | null;
  taskSummary: string | null;
}
/**
 * Structured model output has no authority to select a reviewer or evidence snapshot.
 */
export interface ReviewProposals {
  /**
   * @maxItems 32
   */
  candidates: ReviewCandidate[];
}
/**
 * A model proposal cannot assign itself verification or a final disposition.
 */
export interface ReviewCandidate {
  /**
   * @minItems 1
   * @maxItems 16
   */
  artifact_ids: WorkflowIdentifier[];
  candidate_id: WorkflowIdentifier;
  category: ReviewCategory;
  condition: WorkflowIdentifier;
  severity: ReviewSeverity;
  summary: ResearchText;
}
/**
 * One bounded task suggestion derived from the authoritative session state.
 */
export interface ProjectionSuggestion {
  kind: "task" | "follow-up" | "recovery";
  label: string;
  prompt: string;
  suggestionId:
    | "inspect-project"
    | "plan-project"
    | "continue-plan"
    | "review-changes"
    | "verify-work"
    | "recover-task"
    | "identify-next-step";
}
export interface ProjectionTask {
  status: "todo" | "in-progress" | "done";
  statusLabel: string;
  title: string;
}
export interface ProjectionUsage {
  accumulatedCost: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  callCount: number;
  completionTokens: number;
  contextWindow: number | null;
  modelName: string;
  promptTokens: number;
  purposeLabel: string;
  reasoningTokens: number;
  usageId: string;
}
/**
 * Authoritative stage snapshot stored in the paired session and audit journal.
 */
export interface WorkflowRun {
  binding: WorkflowProjectBinding;
  completed: WorkflowStageEvaluation[];
  created_at: string;
  evaluation: WorkflowStageEvaluation | null;
  phase: "ready" | "running" | "review" | "blocked" | "completed" | "cancelled";
  research_review: ResearchReviewRun | null;
  revision: number;
  run_id: string;
  stage_id: WorkflowIdentifier;
  stage_started_at: string | null;
  stage_usage_baseline: ExecutionUsage | null;
  started_sequence: number | null;
}
/**
 * Project-relative inputs and output location; never an external workspace root.
 */
export interface WorkflowProjectBinding {
  /**
   * @minItems 1
   * @maxItems 32
   */
  inputs: WorkflowBoundInput[];
  output_directory: string;
  workflow_fingerprint: string;
  workflow_id: WorkflowIdentifier;
}
/**
 * Private researcher input, bound to the bytes accepted at preparation.
 */
export interface WorkflowBoundInput {
  input_id: WorkflowIdentifier;
  kind: "file" | "text";
  sha256: string;
  value: WorkflowInputValue;
}
/**
 * Content-minimized checks and assessment, not permission to advance a stage.
 */
export interface WorkflowStageEvaluation {
  artifacts: WorkflowValueFingerprint[];
  assessment: WorkflowStageAssessment;
  checks: WorkflowCheckResult[];
}
/**
 * Digest of one bound input or output, without retaining its content.
 */
export interface WorkflowValueFingerprint {
  artifact_id: WorkflowIdentifier;
  sha256: string;
}
/**
 * Evidence eligibility, separate from the researcher's permission to advance.
 */
export interface WorkflowStageAssessment {
  evidence_fingerprint: string;
  evidence_satisfied: boolean;
  reasons: string[];
  researcher_review_required: boolean;
  stage_id: WorkflowIdentifier;
  workflow_fingerprint: string;
}
/**
 * Gateway evaluator result bound to the exact inputs it inspected.
 */
export interface WorkflowCheckResult {
  check_id: WorkflowIdentifier;
  evaluator_id: WorkflowIdentifier;
  inspected: WorkflowValueFingerprint[];
  status: "passed" | "failed" | "not_run";
}
/**
 * Pre-dispatch evidence and native reviewer results retained by the workflow journal.
 */
export interface ResearchReviewRun {
  assessment: ReviewAssessment | null;
  review_id: Reference;
  /**
   * @minItems 1
   * @maxItems 16
   */
  reviewer_ids: WorkflowIdentifier[];
  snapshot: ReviewSnapshot;
  started_sequence: number;
  status: "pending" | "assessed" | "unavailable" | "cancelled";
  /**
   * @maxItems 16
   */
  submissions: ReviewSubmission[];
  unavailable_reason:
    ("incomplete-review" | "invalid-review" | "no-structured-outcome") | null;
}
/**
 * A deterministic evidence projection, not an approval or a quality benchmark.
 */
export interface ReviewAssessment {
  /**
   * @maxItems 512
   */
  findings: ReviewFinding[];
  schema_version: "heartwood.review-assessment.v1";
  snapshot_sha256: Digest;
}
/**
 * One deduplicated observation; verification never grants action permission.
 */
export interface ReviewFinding {
  category: ReviewCategory;
  condition: WorkflowIdentifier;
  disposition: "open" | "not_actionable";
  evidence: ReviewArtifact[];
  finding_id: Digest;
  reason: WorkflowIdentifier;
  severity: ReviewSeverity;
  /**
   * @minItems 1
   * @maxItems 512
   */
  sources: ReviewSource[];
  verification: ReviewVerification;
  verified_claim: ResearchText | null;
}
/**
 * A named evidence role bound to one observed project file.
 */
export interface ReviewArtifact {
  artifact_id: WorkflowIdentifier;
  file: ExperimentFile;
}
/**
 * Retain each advisory claim without confusing it with verified evidence.
 */
export interface ReviewSource {
  candidate: ReviewCandidate;
  review_id: Reference;
  reviewer_id: Reference;
}
/**
 * Exact file context authorized for a review, without its contents.
 */
export interface ReviewSnapshot {
  /**
   * @minItems 1
   * @maxItems 32
   */
  artifacts: ReviewArtifact[];
  schema_version: "heartwood.review-snapshot.v1";
}
/**
 * Gateway-associated reviewer output for one immutable review context.
 */
export interface ReviewSubmission {
  /**
   * @maxItems 32
   */
  candidates: ReviewCandidate[];
  review_id: Reference;
  reviewer_id: Reference;
  schema_version: "heartwood.review-submission.v1";
  snapshot_sha256: Digest;
}
/**
 * Observed consumption; unavailable provider measurements remain unknown.
 */
export interface ExecutionUsage {
  elapsed_seconds: number;
  input_tokens?: number | null;
  model_calls?: number | null;
  output_tokens?: number | null;
  proposed_actions?: number | null;
  reported_cost_usd?: number | null;
}
/**
 * A presentation affordance carrying the exact revision-bound command to submit.
 */
export interface WorkflowControl {
  control_id:
    "run" | "evaluate" | "accept" | "decline" | "cancel" | "request-review";
  label: WorkflowText;
  request: WorkflowTransition | WorkflowReview;
}
/**
 * Apply a transition only to the exact run and revision the researcher saw.
 */
export interface WorkflowTransition {
  action: "run" | "evaluate" | "cancel" | "request-review";
  revision: number;
  run_id: string;
}
/**
 * Accept or reject checked stage evidence, never the underlying tool actions.
 */
export interface WorkflowReview {
  action: "review";
  approved: boolean;
  evidence_fingerprint: string;
  revision: number;
  run_id: string;
}
