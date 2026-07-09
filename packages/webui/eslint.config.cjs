/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

const {
  getEslintReactConfig,
} = require("@stanfordspezi/spezi-web-configurations");

module.exports = [
  ...getEslintReactConfig({ tsconfigRootDir: __dirname }),
  {
    ignores: ["dist/**/*", "coverage/**/*", "playwright-report/**/*"],
  },
  {
    files: ["scripts/*.cjs"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: {
        URL: "readonly",
        __dirname: "readonly",
        console: "readonly",
        fetch: "readonly",
        process: "readonly",
        require: "readonly",
        setTimeout: "readonly",
      },
      sourceType: "commonjs",
    },
  },
  {
    rules: {
      "import/no-default-export": "off",
      "prefer-arrow/prefer-arrow-functions": "off",
    },
  },
];
