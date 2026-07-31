/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

class ResizeObserverStub implements ResizeObserver {
  disconnect = vi.fn();

  observe = vi.fn();

  unobserve = vi.fn();
}

globalThis.ResizeObserver = ResizeObserverStub;
Element.prototype.scrollIntoView = vi.fn();

const emptyRectList = (): DOMRectList => {
  const rectangles: DOMRect[] = [];
  return Object.assign(rectangles, {
    item: (index: number) => rectangles[index] ?? null,
  });
};

Range.prototype.getClientRects = emptyRectList;
Range.prototype.getBoundingClientRect = () => new DOMRect();
