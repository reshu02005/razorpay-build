'use client';

/**
 * Light / dark switch.
 *
 * The theme itself is applied by the blocking script in `app/layout.tsx`, which
 * runs before the first paint. This component only *changes* it, and it reads
 * the current value from the DOM rather than from its own state on mount - the
 * script is the single source of truth for "what theme am I in right now", so
 * there is no second copy of that decision to drift.
 *
 * Why the mounted guard: the server has no idea what is in `localStorage`, so it
 * cannot know whether to render a sun or a moon. Rendering either one would be a
 * hydration mismatch on half of all loads. Instead the button renders inert and
 * unlabelled for one frame and fills in immediately after mount, which costs
 * nothing visually because it occupies the same box either way.
 */

import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";

import { Button } from "@/components/ui/button";

/** Must match the key read by the bootstrap script in `app/layout.tsx`. */
const THEME_STORAGE_KEY = "recoverai-theme";

export function ThemeToggle() {
  const [isDark, setIsDark] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setIsDark(document.documentElement.classList.contains("dark"));
    setMounted(true);
  }, []);

  function toggle(): void {
    const next = !isDark;
    setIsDark(next);

    const root = document.documentElement;
    root.classList.toggle("dark", next);
    // Keeps native widgets - scrollbars, the text caret, form controls - in step
    // with the page. Without it a dark page still draws light scrollbars.
    root.style.colorScheme = next ? "dark" : "light";

    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next ? "dark" : "light");
    } catch {
      // Storage can be blocked entirely. The theme still applies for this page
      // view; it just will not survive a reload. That is a fair trade against
      // taking the whole header down with an exception.
    }
  }

  if (!mounted) {
    return (
      <Button
        variant="ghost"
        size="icon"
        disabled
        aria-hidden="true"
        className="shrink-0"
        // No accessible name yet: until we know the theme, "Switch to dark mode"
        // would be a 50/50 guess announced with full confidence.
      />
    );
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggle}
      className="shrink-0"
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
    >
      {isDark ? (
        <Sun className="h-4 w-4" aria-hidden="true" />
      ) : (
        <Moon className="h-4 w-4" aria-hidden="true" />
      )}
    </Button>
  );
}
