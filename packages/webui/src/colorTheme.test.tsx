/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  colorThemeStorageKey,
  type ColorTheme,
  useColorTheme,
} from "./colorTheme";

class ThemeMediaQuery extends EventTarget {
  readonly media = "(prefers-color-scheme: dark)";

  matches: boolean;

  constructor(matches: boolean) {
    super();
    this.matches = matches;
  }

  setMatches(matches: boolean) {
    this.matches = matches;
    const event = new Event("change") as MediaQueryListEvent;
    Object.defineProperties(event, {
      matches: { value: matches },
      media: { value: this.media },
    });
    this.dispatchEvent(event);
  }
}

const ThemeControl = () => {
  const { theme, toggleTheme } = useColorTheme();
  return (
    <button type="button" onClick={toggleTheme}>
      {theme}
    </button>
  );
};

const installSystemTheme = (theme: ColorTheme) => {
  const query = new ThemeMediaQuery(theme === "dark");
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => query),
  );
  return query;
};

const storageValues = new Map<string, string>();
const memoryStorage: Storage = {
  get length() {
    return storageValues.size;
  },
  clear: () => storageValues.clear(),
  getItem: (key) => storageValues.get(key) ?? null,
  key: (index) => [...storageValues.keys()][index] ?? null,
  removeItem: (key) => storageValues.delete(key),
  setItem: (key, value) => storageValues.set(key, value),
};

describe("useColorTheme", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", memoryStorage);
    window.localStorage.clear();
    delete document.documentElement.dataset.theme;
    document.documentElement.style.colorScheme = "";
  });

  afterEach(() => vi.unstubAllGlobals());

  it("uses and follows the system theme until the researcher chooses one", () => {
    const query = installSystemTheme("dark");
    render(<ThemeControl />);

    expect(screen.getByRole("button")).toHaveTextContent("dark");
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(window.localStorage.getItem(colorThemeStorageKey)).toBeNull();

    act(() => query.setMatches(false));
    expect(screen.getByRole("button")).toHaveTextContent("light");
    expect(document.documentElement.style.colorScheme).toBe("light");
  });

  it("persists an explicit theme and ignores later system changes", () => {
    const query = installSystemTheme("light");
    render(<ThemeControl />);

    fireEvent.click(screen.getByRole("button", { name: "light" }));
    expect(screen.getByRole("button")).toHaveTextContent("dark");
    expect(window.localStorage.getItem(colorThemeStorageKey)).toBe("dark");

    act(() => query.setMatches(false));
    expect(screen.getByRole("button")).toHaveTextContent("dark");
  });

  it("restores a saved preference instead of the system theme", () => {
    window.localStorage.setItem(colorThemeStorageKey, "light");
    installSystemTheme("dark");
    render(<ThemeControl />);

    expect(screen.getByRole("button")).toHaveTextContent("light");
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
  });
});
