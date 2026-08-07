/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { languages } from "@codemirror/language-data";
import { MergeView } from "@codemirror/merge";
import { EditorState, type Extension } from "@codemirror/state";
import { EditorView, minimalSetup } from "codemirror";
import { useEffect, useRef } from "react";

interface CodeViewerProps {
  content: string;
  path: string;
}

interface DiffViewerProps {
  modified: string;
  original: string;
  path: string;
}

const readOnlyExtensions = (label: string): Extension[] => [
  minimalSetup,
  EditorState.readOnly.of(true),
  EditorView.editable.of(false),
  EditorView.contentAttributes.of({ "aria-label": label }),
  EditorView.theme({
    "&": {
      height: "100%",
      backgroundColor: "#fff",
      color: "#18241d",
      fontSize: "13px",
    },
    ".cm-content": {
      fontFamily:
        '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
      padding: "12px 0",
    },
    ".cm-gutters": {
      backgroundColor: "#f7f9f7",
      borderRight: "1px solid #e1e5e2",
      color: "#758179",
    },
    ".cm-scroller": {
      overflow: "auto",
    },
  }),
];

export const CodeViewer = ({ content, path }: CodeViewerProps) => {
  const parentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    let view: EditorView | null = null;
    void loadLanguage(path)
      .catch(() => null)
      .then((language) => {
        if (!active || parentRef.current === null) return;
        view = new EditorView({
          doc: content,
          extensions: [
            ...readOnlyExtensions(`Read-only file contents: ${path}`),
            ...(language === null ? [] : [language]),
          ],
          parent: parentRef.current,
        });
        makeScrollerAccessible(view, `Scrollable file contents: ${path}`);
      });
    return () => {
      active = false;
      view?.destroy();
    };
  }, [content, path]);

  return (
    <div
      aria-label={`Read-only file: ${path}`}
      className="code-viewer"
      ref={parentRef}
      role="region"
    />
  );
};

export const DiffViewer = ({ modified, original, path }: DiffViewerProps) => {
  const parentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    let view: MergeView | null = null;
    void loadLanguage(path)
      .catch(() => null)
      .then((language) => {
        if (!active || parentRef.current === null) return;
        const languageExtensions = language === null ? [] : [language];
        view = new MergeView({
          a: {
            doc: original,
            extensions: [
              ...readOnlyExtensions(`Original file contents: ${path}`),
              ...languageExtensions,
            ],
          },
          b: {
            doc: modified,
            extensions: [
              ...readOnlyExtensions(`Modified file contents: ${path}`),
              ...languageExtensions,
            ],
          },
          collapseUnchanged: { margin: 3, minSize: 8 },
          diffConfig: { scanLimit: 1_000, timeout: 1_000 },
          gutter: true,
          highlightChanges: true,
          orientation: "a-b",
          parent: parentRef.current,
        });
        makeScrollerAccessible(
          view.a,
          `Scrollable original file contents: ${path}`,
        );
        makeScrollerAccessible(
          view.b,
          `Scrollable modified file contents: ${path}`,
        );
      });
    return () => {
      active = false;
      view?.destroy();
    };
  }, [modified, original, path]);

  return (
    <div
      aria-label={`Read-only change: ${path}`}
      className="code-viewer diff-viewer"
      ref={parentRef}
      role="region"
    />
  );
};

const makeScrollerAccessible = (view: EditorView, label: string): void => {
  view.scrollDOM.tabIndex = 0;
  view.scrollDOM.setAttribute("aria-label", label);
};

const loadLanguage = async (path: string) => {
  const name = path.split("/").at(-1) ?? path;
  const extension = name.includes(".") ? name.split(".").at(-1) : undefined;
  const description = languages.find(
    (candidate) =>
      candidate.filename?.test(name) === true ||
      (extension !== undefined && candidate.extensions.includes(extension)),
  );
  return description === undefined ? null : description.load();
};
