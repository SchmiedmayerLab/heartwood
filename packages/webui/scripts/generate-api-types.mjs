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
import { dirname, join, resolve } from "node:path";

import { compile } from "json-schema-to-typescript";
import { format, resolveConfig } from "prettier";

const scriptDirectory = dirname(resolve(process.argv[1]));
const repositoryRoot = resolve(scriptDirectory, "../../..");
const schemaExporter = join(
  repositoryRoot,
  "packages/schemas/scripts/export_api_schema.py",
);
const generatedTypes = join(
  repositoryRoot,
  "packages/webui/src/apiTypes.generated.ts",
);
const checkOnly = process.argv.includes("--check");

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
const exported = spawnSync(python, [schemaExporter], {
  cwd: repositoryRoot,
  encoding: "utf8",
});

if (exported.error !== undefined || exported.status !== 0) {
  const reason =
    exported.error?.message ?? exported.stderr.trim() ?? "unknown error";
  throw new Error(`Unable to export the Heartwood API schema: ${reason}`);
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
 * Generated from the public Pydantic API contract.
 * Run \`npm run contracts:generate\` after changing a shared request or response.
 */
`;
const compiled = await compile(schema, "HeartwoodApiContract", {
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
const prettierConfig = (await resolveConfig(generatedTypes)) ?? {};
const output = await format(compiled, {
  ...prettierConfig,
  filepath: generatedTypes,
});

if (checkOnly) {
  const current = await readFile(generatedTypes, "utf8").catch(() => "");
  if (current !== output) {
    throw new Error(
      "Generated browser API types are stale. Run `npm run contracts:generate`.",
    );
  }
} else {
  await writeFile(generatedTypes, output, "utf8");
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
