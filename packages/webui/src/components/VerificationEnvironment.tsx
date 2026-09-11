/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { Button } from "@schmiedmayerlab/grove-design-system/components/Button";
import { Download } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { HeartwoodClient } from "../client";
import { downloadTextFile } from "../downloads";

export const VerificationEnvironment = ({
  client,
}: {
  client: Pick<HeartwoodClient, "getVerificationEnvironment">;
}) => {
  const generationRef = useRef(0);
  const [state, setState] = useState<{
    client: typeof client;
    pending: boolean;
    error?: string;
  } | null>(null);
  useEffect(
    () => () => {
      generationRef.current += 1;
    },
    [client],
  );
  const current = state?.client === client ? state : null;
  const capture = async () => {
    const request = ++generationRef.current;
    setState({ client, pending: true });
    try {
      const snapshot = await client.getVerificationEnvironment();
      if (request !== generationRef.current) return;
      downloadTextFile(
        "python-environment.json",
        JSON.stringify(snapshot, null, 2) + "\n",
      );
      setState({ client, pending: false });
    } catch (error: unknown) {
      if (request === generationRef.current)
        setState({
          client,
          pending: false,
          error:
            error instanceof Error ?
              error.message
            : "Python environment unavailable",
        });
    }
  };
  return (
    <>
      <Button
        type="button"
        variant="outline"
        isPending={current?.pending ?? false}
        disabled={current?.pending ?? false}
        onClick={() => {
          void capture();
        }}
      >
        <Download size={16} />
        Export Verification Environment
      </Button>
      {current?.pending && (
        <span role="status">Inspecting Python environment</span>
      )}
      {current?.error && <span role="alert">{current.error}</span>}
    </>
  );
};
