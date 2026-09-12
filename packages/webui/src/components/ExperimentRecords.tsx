/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Button } from "@schmiedmayerlab/grove-design-system/components/Button";
import { Download, LoaderCircle, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { HeartwoodClient } from "../client";
import { downloadTextFile } from "../downloads";
import type { ExperimentCollection, ExperimentRun } from "../types";

const emptyLabels: Record<string, string> = {};
type RecordClient = Pick<
  HeartwoodClient,
  "getExperimentRecords" | "getExperimentExport"
>;

export const ExperimentRunList = ({
  runs,
  stageLabels = emptyLabels,
}: {
  runs: ExperimentRun[];
  stageLabels?: Record<string, string>;
}) => (
  <>
    {runs.map((run) => {
      const stage = run.definition.stage;
      const label =
        stage ?
          (stageLabels[stage.stage_id] ?? stage.stage_id)
        : run.definition.entry_point;
      return (
        <section
          key={run.run_id}
          aria-label={`Experiment ${label ?? run.run_id}`}
        >
          <h4>{label ?? run.run_id}</h4>
          <p>
            {run.status === "started" || run.status === "resumed" ?
              "Outcome not recorded"
            : run.status}
          </p>
          <dl>
            <dt>Run</dt>
            <dd>
              <code>{run.run_id}</code>
            </dd>
            {stage ?
              <>
                <dt>Session</dt>
                <dd>
                  <code>{stage.session_id}</code>
                </dd>
              </>
            : null}
            <dt>Attempt</dt>
            <dd>{run.attempt}</dd>
            <dt>Environment Digest</dt>
            <dd>
              <code>{run.definition.environment.sha256}</code>
            </dd>
            <dt>Environment Evidence</dt>
            <dd>{run.definition.environment.source}</dd>
            <dt>Linked Events</dt>
            <dd>{run.evidence.length}</dd>
          </dl>
          <ul>
            {run.outputs.map((file) => (
              <li key={file.path}>
                <code>{file.path}</code>
                <br />
                <code>{file.sha256}</code>
              </li>
            ))}
          </ul>
          <details>
            <summary>Inputs and Code</summary>
            {(["inputs", "code"] as const).map((kind) => (
              <div key={kind}>
                <h5>{kind === "inputs" ? "Inputs" : "Code"}</h5>
                <ul>
                  {run.definition[kind].map((file) => (
                    <li key={file.path}>
                      <code>{file.path}</code>
                      <br />
                      <code>{file.sha256}</code>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            <dl>
              <dt>Parameters Digest</dt>
              <dd>
                <code>{run.definition.parameters_sha256}</code>
              </dd>
            </dl>
          </details>
        </section>
      );
    })}
  </>
);

export const ProjectExperimentRecords = ({
  client,
  revision,
}: {
  client: RecordClient;
  revision: string;
}) => {
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const request = useMemo(
    () => ({ client, revision, attempt, open }),
    [client, revision, attempt, open],
  );
  const [loaded, setLoaded] = useState<{
    request: typeof request;
    records?: ExperimentCollection;
    error?: string;
  } | null>(null);
  const current = loaded?.request === request ? loaded : null;
  const records = current?.records ?? null;
  const [exportState, setExportState] = useState<{
    client: RecordClient;
    pending: boolean;
    digest?: string;
    error?: string;
  } | null>(null);
  const currentExport = exportState?.client === client ? exportState : null;
  const exporting = currentExport?.pending ?? false;
  const exportDigest = currentExport?.digest ?? null;
  const error = current?.error ?? currentExport?.error ?? null;
  const exportGenerationRef = useRef(0);
  useEffect(
    () => () => {
      exportGenerationRef.current += 1;
    },
    [client],
  );
  useEffect(() => {
    if (!request.open) return;
    let active = true;
    void request.client
      .getExperimentRecords()
      .then((value) => {
        if (active) setLoaded({ request, records: value });
      })
      .catch((caught: unknown) => {
        if (active)
          setLoaded({
            request,
            error:
              caught instanceof Error ?
                caught.message
              : "Experiment records unavailable",
          });
      });
    return () => {
      active = false;
    };
  }, [request]);
  const exportRecords = async () => {
    const generation = ++exportGenerationRef.current;
    setExportState({ client, pending: true });
    try {
      const result = await client.getExperimentExport();
      if (generation !== exportGenerationRef.current) return;
      downloadTextFile(
        `heartwood-experiments-${result.sha256.slice(0, 12)}.jsonl`,
        result.jsonl,
      );
      setExportState({ client, pending: false, digest: result.sha256 });
    } catch (caught: unknown) {
      if (generation === exportGenerationRef.current)
        setExportState({
          client,
          pending: false,
          error:
            caught instanceof Error ?
              caught.message
            : "Experiment export unavailable",
        });
    }
  };
  return (
    <details
      className="research-provenance project-experiments"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>Project Experiment Records</summary>
      {open ?
        <>
          <p>Project-local records · Unsigned export</p>
          <div className="research-controls">
            <Button
              variant="outline"
              onClick={() => setAttempt((value) => value + 1)}
              disabled={current === null}
            >
              <RefreshCw size={16} />
              Refresh
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                void exportRecords();
              }}
              disabled={exporting || records === null}
            >
              {exporting ?
                <LoaderCircle className="animate-spin" size={16} />
              : <Download size={16} />}
              Export Records
            </Button>
          </div>
          {error !== null ?
            <p role="alert">{error}</p>
          : null}
          {current === null ?
            <p role="status">
              <LoaderCircle className="animate-spin" size={16} />
              Loading experiment records
            </p>
          : null}
          {records !== null && records.runs.length === 0 ?
            <p>No experiments recorded in this project.</p>
          : null}
          {records !== null ?
            <ExperimentRunList runs={records.runs} />
          : null}
          {exportDigest !== null ?
            <p role="status">
              Export SHA-256: <code>{exportDigest}</code>
            </p>
          : null}
        </>
      : null}
    </details>
  );
};
