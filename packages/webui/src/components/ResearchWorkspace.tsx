/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Button } from "@schmiedmayerlab/grove-design-system/components/Button";
import { Input } from "@schmiedmayerlab/grove-design-system/components/Input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@schmiedmayerlab/grove-design-system/components/Select";
import {
  Check,
  Circle,
  FileText,
  LoaderCircle,
  Play,
  RefreshCw,
} from "lucide-react";
import { useEffect, useState } from "react";
import type { HeartwoodClient } from "../client";
import type {
  SessionProjection,
  WorkflowCatalog,
  WorkflowRequest,
  WorkspaceFile,
} from "../types";
import {
  ExperimentRunList,
  ProjectExperimentRecords,
} from "./ExperimentRecords";
import { displaySafeText, SafeMarkdown } from "./SafeMarkdown";

interface ResearchWorkspaceProps {
  client: Pick<
    HeartwoodClient,
    | "getResearchWorkflows"
    | "getWorkspaceFile"
    | "getExperimentRecords"
    | "getExperimentExport"
  >;
  sessionId: string;
  projection: SessionProjection;
  busy: boolean;
  modelReady: boolean;
  onSubmit: (request: WorkflowRequest) => void;
  onConversation: () => void;
  onNewSession: () => void;
}

export const ResearchWorkspace = (props: ResearchWorkspaceProps) => (
  <>
    <WorkflowWorkspace {...props} />
    <ProjectExperimentRecords
      client={props.client}
      revision={JSON.stringify([
        props.sessionId,
        props.projection.experiments.map((run) => [
          run.run_id,
          run.attempt,
          run.status,
          run.updated_at,
        ]),
      ])}
    />
  </>
);

const WorkflowWorkspace = ({
  client,
  sessionId,
  projection,
  busy,
  modelReady,
  onSubmit,
  onConversation,
  onNewSession,
}: ResearchWorkspaceProps) => {
  const [catalog, setCatalog] = useState<WorkflowCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [workflowId, setWorkflowId] = useState("");
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [output, setOutput] = useState("results");
  const [artifact, setArtifact] = useState<string | null>(null);
  const [preview, setPreview] = useState<{
    path: string;
    revision: number;
    file?: WorkspaceFile;
    error?: string;
  } | null>(null);
  useEffect(() => {
    let active = true;
    void client
      .getResearchWorkflows()
      .then((value) => {
        if (!active) return;
        setCatalog(value);
        setWorkflowId((current) =>
          current.length > 0 ?
            current
          : (value.workflows.find((entry) => entry.available)?.definition
              .workflow_id ?? ""),
        );
      })
      .catch((error: unknown) => {
        if (active)
          setCatalogError(
            error instanceof Error ?
              error.message
            : "Workflow catalog unavailable",
          );
      });
    return () => {
      active = false;
    };
  }, [client, attempt]);

  useEffect(() => {
    if (artifact === null) return;
    let active = true;
    void client
      .getWorkspaceFile(sessionId, artifact)
      .then((file) => {
        if (active)
          setPreview({
            path: artifact,
            revision: projection.workspaceRevision,
            file,
          });
      })
      .catch((error: unknown) => {
        if (active)
          setPreview({
            path: artifact,
            revision: projection.workspaceRevision,
            error:
              error instanceof Error ? error.message : "Artifact unavailable",
          });
      });
    return () => {
      active = false;
    };
  }, [artifact, client, sessionId, projection.workspaceRevision]);

  if (catalogError !== null)
    return (
      <div className="workspace-state" role="alert">
        {catalogError}
        <Button
          variant="outline"
          onClick={() => {
            setCatalogError(null);
            setAttempt((value) => value + 1);
          }}
        >
          <RefreshCw size={16} />
          Retry
        </Button>
      </div>
    );
  if (catalog === null)
    return (
      <div className="workspace-state" role="status">
        <LoaderCircle className="animate-spin" size={16} />
        Loading research workflows
      </div>
    );
  const run = projection.workflow;
  const selected = catalog.workflows.find(
    (entry) =>
      entry.definition.workflow_id === (run?.binding.workflow_id ?? workflowId),
  );
  const definition = selected?.definition;
  if (!definition)
    return (
      <div className="workspace-state">
        No research workflows are available.
      </div>
    );
  if (run === null && projection.conversation.length > 0)
    return (
      <div className="workspace-state">
        <h2>Start a Research Workflow</h2>
        <p>
          This conversation already contains work. Use a new session to keep the
          analysis and its evidence together.
        </p>
        <Button onClick={onNewSession}>New Session</Button>
      </div>
    );
  const stage =
    run ?
      definition.stages.find((item) => item.stage_id === run.stage_id)
    : null;
  const completed = new Set(
    run?.completed.map((item) => item.assessment.stage_id) ?? [],
  );
  return (
    <section className="research-workspace" aria-label="Research workflow">
      <header>
        <h2>{run ? definition.label : "Research Workflows"}</h2>
        <p>{definition.description}</p>
      </header>
      {run === null ?
        <form
          className="research-setup"
          onSubmit={(event) => {
            event.preventDefault();
            if (busy || !modelReady || !selected.available) return;
            onSubmit({
              action: "start",
              workflow_id: definition.workflow_id,
              inputs,
              output_directory: output,
            });
          }}
        >
          <label>
            Workflow
            <Select
              value={workflowId}
              onValueChange={(value) => {
                setWorkflowId(value);
                setInputs({});
              }}
            >
              <SelectTrigger aria-label="Workflow">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {catalog.workflows.map((entry) => (
                  <SelectItem
                    key={entry.definition.workflow_id}
                    value={entry.definition.workflow_id}
                    disabled={!entry.available}
                  >
                    {entry.definition.label}
                    {entry.available ? "" : " (not yet available)"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          {definition.inputs.map((input) => (
            <div className="research-field" key={input.input_id}>
              <label htmlFor={`research-${input.input_id}`}>
                {input.label}
              </label>
              {input.kind === "text" ?
                <textarea
                  id={`research-${input.input_id}`}
                  aria-describedby={`research-description-${input.input_id}`}
                  required
                  value={inputs[input.input_id] ?? ""}
                  onChange={(event) =>
                    setInputs({
                      ...inputs,
                      [input.input_id]: event.target.value,
                    })
                  }
                />
              : <Input
                  id={`research-${input.input_id}`}
                  aria-describedby={`research-description-${input.input_id}`}
                  required
                  value={inputs[input.input_id] ?? ""}
                  onChange={(event) =>
                    setInputs({
                      ...inputs,
                      [input.input_id]: event.target.value,
                    })
                  }
                />
              }
              <small id={`research-description-${input.input_id}`}>
                {input.description}
              </small>
            </div>
          ))}
          <label htmlFor="research-output">
            Output Folder
            <Input
              id="research-output"
              required
              value={output}
              onChange={(event) => setOutput(event.target.value)}
            />
          </label>
          <details>
            <summary>Work Limits</summary>
            <dl>
              <dt>Model Calls</dt>
              <dd>{definition.budget.maximum_model_calls}</dd>
              <dt>Proposed Actions</dt>
              <dd>{definition.budget.maximum_actions}</dd>
              <dt>Elapsed Time</dt>
              <dd>{definition.budget.maximum_seconds / 60} minutes</dd>
            </dl>
            <p>
              Observed limits are checked between operations; an in-flight model
              request can finish before a pause.
            </p>
          </details>
          <Button
            type="submit"
            disabled={busy || !modelReady || !selected.available}
          >
            <Play size={16} />
            Start Workflow
          </Button>
          {!modelReady ?
            <p role="status">Configure and connect a model before starting.</p>
          : null}
        </form>
      : <>
          <ol className="research-stages" aria-label="Workflow stages">
            {definition.stages.map((item) => (
              <li
                key={item.stage_id}
                aria-current={
                  item.stage_id === run.stage_id ? "step" : undefined
                }
              >
                {completed.has(item.stage_id) ?
                  <Check size={16} />
                : <Circle size={16} />}
                <span>{item.label}</span>
                <small>
                  {completed.has(item.stage_id) ?
                    "Complete"
                  : item.stage_id === run.stage_id ?
                    run.phase
                  : "Not started"}
                </small>
              </li>
            ))}
          </ol>
          <div className="research-stage-status" role="status">
            <h3>{stage?.label}</h3>
            <p>
              {run.phase === "review" ?
                "Review the stage artifacts and checks before accepting. Acceptance does not approve future tool actions."
              : run.phase === "blocked" ?
                "The stage cannot advance. Inspect the checks and review decision before continuing in the conversation."
              : run.phase === "completed" ?
                "The workflow is complete. Inspect the report and retain the audit export with your analysis."
              : run.phase === "cancelled" ?
                "The workflow was cancelled. Its recorded work remains available."
              : run.phase === "running" ?
                "The agent is working or awaiting action review. Check results when the stage finishes."
              : "Ready to run the next stage."}
            </p>
          </div>
          {run.evaluation ?
            <ul className="research-checks" aria-label="Stage checks">
              {run.evaluation.checks.map((check) => (
                <li key={check.check_id}>
                  {stage?.checks.find(
                    (item) => item.check_id === check.check_id,
                  )?.description ?? check.check_id}
                  <strong>{check.status.replaceAll("_", " ")}</strong>
                </li>
              ))}
            </ul>
          : null}
          <div className="research-controls">
            {projection.workflowControls.map((control) => (
              <Button
                key={control.control_id}
                variant={
                  (
                    control.control_id === "run" ||
                    control.control_id === "accept"
                  ) ?
                    "default"
                  : "outline"
                }
                disabled={busy}
                onClick={() => onSubmit(control.request)}
              >
                {control.label}
              </Button>
            ))}
            <Button variant="outline" onClick={onConversation}>
              {projection.pendingApproval ?
                "Review Agent Actions"
              : "Open Conversation"}
            </Button>
          </div>
          {run.research_review ?
            <details className="research-provenance">
              <summary>Research Review: {run.research_review.status}</summary>
              {run.research_review.unavailable_reason ?
                <p>
                  {run.research_review.unavailable_reason.replaceAll("-", " ")}
                </p>
              : null}
              {run.research_review.assessment ?
                <ul
                  className="research-checks"
                  aria-label="Research review findings"
                >
                  {run.research_review.assessment.findings.map((finding) => (
                    <li key={finding.finding_id}>
                      {displaySafeText(
                        finding.verified_claim ?? finding.reason,
                      )}
                      <strong>{finding.verification}</strong>
                    </li>
                  ))}
                </ul>
              : null}
              <p>
                Review findings do not authorize changes or establish scientific
                correctness.
              </p>
            </details>
          : null}
          <h3>Analysis Artifacts</h3>
          {projection.experiments.length > 0 ?
            <details className="research-provenance">
              <summary>Experiment Records</summary>
              <ExperimentRunList
                runs={projection.experiments}
                stageLabels={Object.fromEntries(
                  definition.stages.map((stage) => [
                    stage.stage_id,
                    stage.label,
                  ]),
                )}
              />
            </details>
          : null}
          <div className="research-artifacts">
            {run.binding.artifacts.map((item) => (
              <Button
                key={item.artifact_id}
                variant="ghost"
                onClick={() => setArtifact(item.path)}
              >
                <FileText size={16} />
                {definition.artifacts.find(
                  (definition) => definition.artifact_id === item.artifact_id,
                )?.label ?? item.artifact_id}
              </Button>
            ))}
          </div>
          {artifact !== null ?
            <section className="research-preview" aria-label="Artifact preview">
              <h3>{artifact}</h3>
              {(
                preview?.path !== artifact ||
                preview.revision !== projection.workspaceRevision
              ) ?
                <p role="status">Loading artifact...</p>
              : preview.error ?
                <p role="alert">{preview.error}</p>
              : preview.file?.content == null ?
                <p>
                  {preview.file?.message ??
                    "This artifact is not available yet."}
                </p>
              : <>
                  {preview.file.status === "truncated" ?
                    <p role="status">Only part of this artifact is shown.</p>
                  : null}
                  {artifact.endsWith(".md") ?
                    <SafeMarkdown content={preview.file.content} />
                  : <pre>{preview.file.content}</pre>}
                </>
              }
            </section>
          : null}
        </>
      }
    </section>
  );
};
