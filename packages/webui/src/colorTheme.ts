/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import { useCallback, useEffect, useState } from "react";

export type ColorTheme = "dark" | "light";

export const colorThemeStorageKey = "heartwood.color-theme";
const darkSchemeQuery = "(prefers-color-scheme: dark)";

const storedTheme = (): ColorTheme | null => {
  try {
    const value = window.localStorage.getItem(colorThemeStorageKey);
    return value === "dark" || value === "light" ? value : null;
  } catch {
    return null;
  }
};

const systemTheme = (): ColorTheme =>
  window.matchMedia(darkSchemeQuery).matches ? "dark" : "light";

const applyTheme = (theme: ColorTheme) => {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
};

const initialTheme = (): ColorTheme => {
  const theme = storedTheme() ?? systemTheme();
  applyTheme(theme);
  return theme;
};

export const useColorTheme = () => {
  const [theme, setTheme] = useState<ColorTheme>(initialTheme);

  useEffect(() => {
    if (storedTheme() !== null) return;
    const mediaQuery = window.matchMedia(darkSchemeQuery);
    const updateSystemTheme = (event: MediaQueryListEvent) => {
      if (storedTheme() !== null) return;
      const nextTheme = event.matches ? "dark" : "light";
      applyTheme(nextTheme);
      setTheme(nextTheme);
    };
    mediaQuery.addEventListener("change", updateSystemTheme);
    return () => mediaQuery.removeEventListener("change", updateSystemTheme);
  }, []);

  const toggleTheme = useCallback(() => {
    const nextTheme = theme === "light" ? "dark" : "light";
    applyTheme(nextTheme);
    try {
      window.localStorage.setItem(colorThemeStorageKey, nextTheme);
    } catch {
      // The active theme still applies when browser storage is unavailable.
    }
    setTheme(nextTheme);
  }, [theme]);

  return { theme, toggleTheme };
};
