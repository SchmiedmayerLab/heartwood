/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  displayBoundedSafeText,
  displaySafeText,
  SafeMarkdown,
} from "./SafeMarkdown";

describe("SafeMarkdown", () => {
  it("renders the supported GitHub-Flavored Markdown structure", () => {
    render(
      <SafeMarkdown
        content={`# Result

- one
- two

> Review this result.

| Measure | Value |
| --- | ---: |
| Cohort | 42 |

\`inline\`

\`\`\`python
print("synthetic")
\`\`\``}
      />,
    );

    expect(
      screen.getByRole("heading", { level: 2, name: "Result" }),
    ).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("table")).toBeVisible();
    expect(
      screen.getByRole("region", { name: "Scrollable data table" }),
    ).toHaveAttribute("tabindex", "0");
    expect(screen.getByText("Review this result.")).toBeVisible();
    expect(screen.getByText('print("synthetic")')).toBeVisible();
  });

  it("normalizes model-authored heading levels within the page hierarchy", () => {
    render(<SafeMarkdown content={"## Result\n\n#### Detail"} />);

    expect(
      screen.getByRole("heading", { level: 2, name: "Result" }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { level: 3, name: "Detail" }),
    ).toBeVisible();
  });

  it("removes raw HTML and unsafe link protocols", () => {
    const { container } = render(
      <SafeMarkdown
        content={`<script>alert("unsafe")</script>

[unsafe](javascript:alert(1)) [relative](/credential) [fragment](#result) [safe](https://example.org/result)`}
      />,
    );

    expect(container.querySelector("script")).toBeNull();
    expect(screen.queryByText(/alert\("unsafe"\)/u)).toBeNull();
    expect(screen.getByText("unsafe")).not.toHaveAttribute("href");
    expect(screen.getByText("relative")).not.toHaveAttribute("href");
    expect(screen.getByText("fragment")).not.toHaveAttribute("href");
    expect(screen.getByRole("link", { name: "safe" })).toHaveAttribute(
      "href",
      "https://example.org/result",
    );
    expect(screen.getByRole("link", { name: "safe" })).toHaveAttribute(
      "rel",
      "noreferrer noopener",
    );
  });

  it("never loads model-provided images", () => {
    const { container } = render(
      <SafeMarkdown content="![private result](https://example.org/track.png)" />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("Image omitted: private result")).toBeVisible();
  });

  it("makes invisible display controls explicit and bounds large responses", () => {
    const { container } = render(
      <SafeMarkdown content={`safe\u202Etxt ${"a".repeat(200_001)}`} />,
    );

    expect(within(container).getByText(/safe\\u202etxt/u)).toBeVisible();
    expect(
      screen.getByText("This response is too large to display completely."),
    ).toBeVisible();
  });
});

describe("displaySafeText", () => {
  it("preserves useful whitespace while escaping control characters", () => {
    expect(displaySafeText("line one\nline\t\u0000\u2066two")).toBe(
      "line one\nline\t\\x00\\u2066two",
    );
  });

  it("bounds transient text without parsing it as Markdown", () => {
    expect(displayBoundedSafeText("**still streaming**")).toEqual({
      text: "**still streaming**",
      truncated: false,
    });
    expect(displayBoundedSafeText("a".repeat(200_001))).toMatchObject({
      truncated: true,
    });
  });
});
