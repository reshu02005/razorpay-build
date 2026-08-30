/**
 * Root layout - the frame every route in the console renders inside.
 *
 * Three things happen here and nowhere else:
 *
 * 1.  The document metadata (tab title, description) that a reviewer sees
 *     before the page has painted anything.
 * 2.  The theme bootstrap script (see below) that decides light or dark
 *     *before* the first paint.
 * 3.  The application shell - header, navigation, theme control - which wraps
 *     every page so that an error boundary or a 404 still renders inside the
 *     product rather than on a bare white page.
 *
 * This file is a server component on purpose: it holds no state and reads no
 * browser API, so none of it needs to ship to the client. The interactive
 * pieces (`Nav`, `ThemeToggle`) opt into the client boundary themselves.
 *
 * Typography is the Tailwind `font-sans` stack from `tailwind.config.ts`, which
 * resolves to the host operating system's UI font. No web font is requested:
 * the project must build and run with no network access, and `next/font/google`
 * fetches at build time.
 */

import type { Metadata } from "next";
import type { ReactNode } from "react";


import "./globals.css";

export const metadata: Metadata = {
  title: "RecoverAI - Revenue Recovery Console",
  description:
    "Classify failed payments, propose a recovery strategy inside hard guardrails, and keep a human in the loop for every rupee.",
};

/**
 * Applies the stored theme before React hydrates.
 *
 * This has to be a blocking inline script rather than an effect. An effect runs
 * after the first paint, so a dark-mode operator would get a full-screen white
 * flash on every single navigation and reload - which, in a console someone is
 * reading at 3am during an incident, is genuinely unpleasant and reads as a
 * broken page.
 *
 * The stored value is the same `recoverai-theme` key the toggle in
 * `components/layout/theme-toggle.tsx` writes. When nothing is stored we defer
 * to the operating system preference rather than forcing light: a first-time
 * visitor should see the theme they already chose at the OS level.
 *
 * Everything is wrapped in try/catch because `localStorage` throws outright in
 * a browser configured to block site data, and a theme preference is never
 * worth breaking the page over.
 */
const THEME_BOOTSTRAP = `(function(){try{var stored=window.localStorage.getItem('recoverai-theme');var dark=stored?stored==='dark':window.matchMedia('(prefers-color-scheme: dark)').matches;var root=document.documentElement;root.classList.toggle('dark',dark);root.style.colorScheme=dark?'dark':'light';}catch(e){}})();`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // `suppressHydrationWarning` is required and is scoped to this one element:
    // the script above mutates <html>'s class list before React hydrates, so the
    // server-rendered markup and the live DOM legitimately differ here. Without
    // it React logs a mismatch for a difference we caused on purpose.
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body className="min-h-screen">
        {children}
      </body>
    </html>
  );
}
