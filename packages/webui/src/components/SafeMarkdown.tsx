/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import type { Heading, Root } from "mdast";
import { Component, type ErrorInfo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeSanitize, { type Options } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { visit } from "unist-util-visit";

const MAX_RENDERED_CHARACTERS = 200_000;
const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

const markdownSchema: Options = {
  allowComments: false,
  allowDoctypes: false,
  attributes: {
    a: ["href"],
    code: [["className", /^language-[a-z0-9_-]+$/u]],
    img: ["alt"],
    input: [["type", "checkbox"], "checked", "disabled"],
    ol: ["start"],
    td: ["align"],
    th: ["align"],
  },
  clobber: ["ariaDescribedBy", "ariaLabelledBy", "id", "name"],
  protocols: {
    href: ["http", "https", "mailto"],
  },
  tagNames: [
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "input",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
  ],
};

const markdownComponents: Components = {
  a: ({ children, href }) =>
    href ?
      <a href={href} rel="noreferrer noopener" target="_blank">
        {children}
      </a>
    : <span>{children}</span>,
  h1: ({ children }) => <h2>{children}</h2>,
  h2: ({ children }) => <h3>{children}</h3>,
  h3: ({ children }) => <h4>{children}</h4>,
  h4: ({ children }) => <h5>{children}</h5>,
  h5: ({ children }) => <h6>{children}</h6>,
  h6: ({ children }) => <h6>{children}</h6>,
  img: ({ alt }) => (
    <span className="markdown-image-placeholder">
      {alt ? `Image omitted: ${alt}` : "Image omitted"}
    </span>
  ),
  pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
  table: ({ children }) => <table tabIndex={0}>{children}</table>,
};

const normalizeHeadingHierarchy =
  () =>
  (tree: Root): void => {
    let firstLevel: Heading["depth"] | null = null;
    let previousLevel = 0;
    visit(tree, "heading", (heading) => {
      firstLevel ??= heading.depth;
      const relativeLevel = Math.max(1, heading.depth - firstLevel + 1);
      const normalizedLevel =
        previousLevel === 0 ? 1 : Math.min(relativeLevel, previousLevel + 1);
      heading.depth = normalizedLevel as Heading["depth"];
      previousLevel = normalizedLevel;
    });
  };

interface SafeMarkdownProps {
  content: string;
}

interface MarkdownBoundaryProps extends SafeMarkdownProps {
  children: ReactNode;
}

interface MarkdownBoundaryState {
  failed: boolean;
}

class MarkdownBoundary extends Component<
  MarkdownBoundaryProps,
  MarkdownBoundaryState
> {
  override state: MarkdownBoundaryState = { failed: false };

  static getDerivedStateFromError(): MarkdownBoundaryState {
    return { failed: true };
  }

  override componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // The model response remains available as escaped plain text below.
  }

  override componentDidUpdate(previous: MarkdownBoundaryProps): void {
    if (this.state.failed && previous.content !== this.props.content) {
      this.setState({ failed: false });
    }
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return <p className="markdown-fallback">{this.props.content}</p>;
    }
    return this.props.children;
  }
}

export const SafeMarkdown = ({ content }: SafeMarkdownProps) => {
  const safeContent = displaySafeText(content);
  const truncated = safeContent.length > MAX_RENDERED_CHARACTERS;
  const rendered =
    truncated ? safeContent.slice(0, MAX_RENDERED_CHARACTERS) : safeContent;
  return (
    <MarkdownBoundary content={rendered}>
      <div className="markdown-content">
        <ReactMarkdown
          components={markdownComponents}
          rehypePlugins={[[rehypeSanitize, markdownSchema]]}
          remarkPlugins={[remarkGfm, normalizeHeadingHierarchy]}
          skipHtml
          urlTransform={safeUrl}
        >
          {rendered}
        </ReactMarkdown>
        {truncated ?
          <p className="markdown-truncated" role="note">
            This response is too large to display completely.
          </p>
        : null}
      </div>
    </MarkdownBoundary>
  );
};

const safeUrl = (value: string): string => {
  const candidate = displaySafeText(value).trim();
  if (candidate.startsWith("#")) return candidate;
  try {
    const parsed = new URL(candidate);
    return SAFE_PROTOCOLS.has(parsed.protocol) ? parsed.toString() : "";
  } catch {
    return "";
  }
};

export const displaySafeText = (value: string): string => {
  let rendered = "";
  for (const character of value) {
    const codepoint = character.codePointAt(0) ?? 0;
    if (character === "\n" || character === "\t") {
      rendered += character;
    } else if (isDisplayControl(codepoint)) {
      rendered +=
        codepoint <= 0xff ? `\\x${hex(codepoint, 2)}`
        : codepoint <= 0xffff ? `\\u${hex(codepoint, 4)}`
        : `\\U${hex(codepoint, 8)}`;
    } else {
      rendered += character;
    }
  }
  return rendered;
};

const isDisplayControl = (codepoint: number): boolean =>
  codepoint <= 0x1f ||
  (codepoint >= 0x7f && codepoint <= 0x9f) ||
  codepoint === 0x00ad ||
  (codepoint >= 0x0600 && codepoint <= 0x0605) ||
  codepoint === 0x061c ||
  codepoint === 0x06dd ||
  codepoint === 0x070f ||
  codepoint === 0x0890 ||
  codepoint === 0x0891 ||
  codepoint === 0x08e2 ||
  codepoint === 0x180e ||
  (codepoint >= 0x200b && codepoint <= 0x200f) ||
  (codepoint >= 0x202a && codepoint <= 0x202e) ||
  (codepoint >= 0x2060 && codepoint <= 0x206f) ||
  codepoint === 0xfeff ||
  (codepoint >= 0xfff9 && codepoint <= 0xfffb) ||
  (codepoint >= 0x1bca0 && codepoint <= 0x1bca3) ||
  (codepoint >= 0x1d173 && codepoint <= 0x1d17a) ||
  (codepoint >= 0xe0000 && codepoint <= 0xe0fff) ||
  (codepoint >= 0xd800 && codepoint <= 0xdfff);

const hex = (value: number, width: number): string =>
  value.toString(16).padStart(width, "0");
