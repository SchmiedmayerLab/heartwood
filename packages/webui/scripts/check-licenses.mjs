/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

/* global console, process */

import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const incompatibleMarkers = [
  "AGPL",
  "BUSL",
  "CC-BY-NC",
  "Commons Clause",
  "GPL",
  "SSPL",
];

const hasIncompatibleLicense = (license) => {
  if (license.includes("LGPL")) {
    return false;
  }
  return incompatibleMarkers.some((marker) => license.includes(marker));
};

const packageFiles = async (directory) => {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    if (entry.name.startsWith(".")) {
      continue;
    }
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      if (entry.name.startsWith("@")) {
        files.push(...(await packageFiles(path)));
      } else if (entry.name !== ".bin") {
        files.push(join(path, "package.json"));
      }
    }
  }
  return files;
};

const main = async () => {
  const packageJsonFiles = await packageFiles("node_modules");
  const failures = [];

  for (const path of packageJsonFiles) {
    try {
      const parsed = JSON.parse(await readFile(path, "utf8"));
      const name = String(parsed.name || "");
      const license = String(parsed.license || parsed.licenses || "");
      if (hasIncompatibleLicense(license)) {
        failures.push(`${name}@${parsed.version}: ${license}`);
      }
    } catch (error) {
      void error;
      continue;
    }
  }

  if (failures.length > 0) {
    console.error("Incompatible npm licenses detected:");
    for (const failure of failures) {
      console.error(`- ${failure}`);
    }
    process.exit(1);
  }

  console.log(`Checked ${packageJsonFiles.length} npm package licenses.`);
};

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
