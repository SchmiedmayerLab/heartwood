/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initializeGatewayAccess } from "./gatewayAccess";
import "./grove.css";
import "./styles.css";

const root = document.getElementById("root");

if (root === null) {
  throw new Error("Heartwood web UI root element is missing.");
}

const application = createRoot(root);
window.addEventListener("hashchange", () => {
  if (new URLSearchParams(window.location.hash.slice(1)).has("launch")) {
    window.location.reload();
  }
});
void initializeGatewayAccess().then(
  () =>
    application.render(
      <StrictMode>
        <App />
      </StrictMode>,
    ),
  () =>
    application.render(
      <main role="alert">
        <h1>Open Heartwood</h1>
        <p>Use a new launch link from your terminal to open this session.</p>
      </main>,
    ),
);
