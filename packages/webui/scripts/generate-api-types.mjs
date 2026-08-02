/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";

import { compile } from "json-schema-to-typescript";
import { format, resolveConfig } from "prettier";

const scriptDirectory = dirname(resolve(process.argv[1]));
const repositoryRoot = resolve(scriptDirectory, "../../..");
const checkOnly = process.argv.includes("--check");
const contracts = [
  {
    exporter: join(
      repositoryRoot,
      "packages/schemas/scripts/export_api_schema.py",
    ),
    output: join(repositoryRoot, "packages/webui/src/apiTypes.generated.ts"),
    rootName: "HeartwoodApiContract",
    sourceLabel: "public Pydantic API contract",
  },
  {
    exporter: join(
      repositoryRoot,
      "packages/gateway/scripts/export_session_projection_schema.py",
    ),
    output: join(
      repositoryRoot,
      "packages/webui/src/sessionProjection.generated.ts",
    ),
    rootName: "SessionProjection",
    sourceLabel: "gateway-owned Pydantic session projection",
    runtimeSchemaOutput: join(
      repositoryRoot,
      "packages/webui/src/sessionProjectionSchema.generated.ts",
    ),
  },
];

const pythonCandidates = [
  process.env.HEARTWOOD_PYTHON,
  join(repositoryRoot, ".venv/bin/python"),
  join(repositoryRoot, ".venv/Scripts/python.exe"),
  "python3",
].filter((candidate) => candidate !== undefined);

const python =
  pythonCandidates.find(
    (candidate) => candidate === "python3" || existsSync(candidate),
  ) ?? "python3";
const staleOutputs = [];

for (const contract of contracts) {
  const exported = spawnSync(python, [contract.exporter], {
    cwd: repositoryRoot,
    encoding: "utf8",
  });
  if (exported.error !== undefined || exported.status !== 0) {
    const processReason =
      exported.status === null ?
        `terminated by ${exported.signal ?? "an unknown signal"}`
      : `exited with status ${exported.status}`;
    const reason =
      exported.error?.message.trim() ||
      exported.stderr?.trim() ||
      processReason;
    throw new Error(`Unable to export ${contract.sourceLabel}: ${reason}`);
  }

  const schema = removeFieldTitles(JSON.parse(exported.stdout));
  const bannerComment = `/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

/* eslint-disable */
/**
 * Generated from the ${contract.sourceLabel}.
 * Run \`npm run contracts:generate\` after changing a shared request or response.
 */
`;
  const compiled = await compile(schema, contract.rootName, {
    additionalProperties: false,
    bannerComment,
    declareExternallyReferenced: true,
    enableConstEnums: false,
    ignoreMinAndMaxItems: true,
    style: {
      printWidth: 100,
      semi: true,
      singleQuote: false,
      trailingComma: "all",
    },
    unknownAny: false,
    unreachableDefinitions: false,
  });
  const prettierConfig = (await resolveConfig(contract.output)) ?? {};
  const output = await format(compiled, {
    ...prettierConfig,
    filepath: contract.output,
  });
  const runtimeSchemaOutput =
    contract.runtimeSchemaOutput === undefined ?
      null
    : await format(
        `${bannerComment.replace("\n/* eslint-disable */", "")}export const sessionProjectionJsonSchema = ${JSON.stringify(minimizeRuntimeSchema(schema))} as const;\n`,
        {
          ...prettierConfig,
          filepath: contract.runtimeSchemaOutput,
        },
      );

  if (checkOnly) {
    const current = await readFile(contract.output, "utf8").catch(() => "");
    if (current !== output) {
      staleOutputs.push(relative(repositoryRoot, contract.output));
    }
    if (runtimeSchemaOutput !== null) {
      const currentSchema = await readFile(
        contract.runtimeSchemaOutput,
        "utf8",
      ).catch(() => "");
      if (currentSchema !== runtimeSchemaOutput) {
        staleOutputs.push(
          relative(repositoryRoot, contract.runtimeSchemaOutput),
        );
      }
    }
  } else {
    await writeFile(contract.output, output, "utf8");
    if (runtimeSchemaOutput !== null) {
      await writeFile(
        contract.runtimeSchemaOutput,
        runtimeSchemaOutput,
        "utf8",
      );
    }
  }
}

if (staleOutputs.length > 0) {
  throw new Error(
    `Generated browser API types are stale: ${staleOutputs.join(", ")}. ` +
      "Run `npm run contracts:generate`.",
  );
}

function removeFieldTitles(value, propertyMap = false) {
  if (Array.isArray(value)) {
    return value.map((item) => removeFieldTitles(item));
  }
  if (value === null || typeof value !== "object") {
    return value;
  }

  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key !== "title" || propertyMap)
      .map(([key, item]) => [
        key,
        removeFieldTitles(item, key === "properties"),
      ]),
  );
}

function minimizeRuntimeSchema(value, propertyMap = false) {
  if (Array.isArray(value)) {
    return value.map((item) => minimizeRuntimeSchema(item));
  }
  if (value === null || typeof value !== "object") {
    return value;
  }

  return Object.fromEntries(
    Object.entries(value)
      .filter(
        ([key]) => propertyMap || (key !== "default" && key !== "description"),
      )
      .map(([key, item]) => [
        key,
        minimizeRuntimeSchema(item, !propertyMap && key === "properties"),
      ]),
  );
}
