"use client";

import { useEffect, useState } from "react";
import { t } from "@/lib/copy";
import type { Locale } from "@/lib/locales";

type Theme = "light" | "dark";

function readTheme(): Theme {
  if (typeof document === "undefined") return "light";
  const attr = document.documentElement.getAttribute("data-theme");
  return attr === "dark" ? "dark" : "light";
}

function applyTheme(next: Theme) {
  document.documentElement.setAttribute("data-theme", next);
  document.documentElement.style.colorScheme = next;
  try {
    localStorage.setItem("vitual-theme", next);
  } catch {
    /* ignore */
  }
}

export function ThemeToggle({ locale }: { locale: Locale }) {
  const copy = t(locale).nav;
  const [theme, setTheme] = useState<Theme>("light");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setTheme(readTheme());
    setReady(true);
  }, []);

  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    applyTheme(next);
  }

  const label = theme === "dark" ? copy.themeToLight : copy.themeToDark;

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={toggle}
      aria-label={label}
      title={label}
      data-theme-ready={ready ? "1" : "0"}
    >
      <span className="theme-toggle-track" aria-hidden="true">
        <span className="theme-toggle-thumb" data-mode={theme} />
      </span>
      <span className="theme-toggle-text">{theme === "dark" ? copy.themeDark : copy.themeLight}</span>
    </button>
  );
}
