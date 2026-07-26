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
    files: ["scripts/*.mjs"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: {
        console: "readonly",
        process: "readonly",
      },
      sourceType: "module",
    },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: [
      "src/client.ts",
      "src/projectionSchema.ts",
      "src/types.ts",
      "src/**/*.test.ts",
      "src/**/*.test.tsx",
      "src/e2e/**/*",
      "src/test/**/*",
    ],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "ImportSpecifier[imported.name='SessionEvent']",
          message:
            "Presentation code must render the gateway-owned SessionProjection instead of reducing raw session events.",
        },
        {
          selector: "Identifier[name='buildViewModel']",
          message:
            "Presentation code must consume SessionProjection instead of defining an interface-local event reducer.",
        },
      ],
    },
  },
  {
    rules: {
      "import/no-default-export": "off",
      "prefer-arrow/prefer-arrow-functions": "off",
    },
  },
];
