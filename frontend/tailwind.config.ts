/**
 * Tailwind theme for the RecoverAI merchant console.
 *
 * Two decisions drive everything in this file:
 *
 * 1.  Colours are declared as CSS custom properties (see `src/app/globals.css`)
 *     and referenced here as `hsl(var(--token))`. The theme therefore has one
 *     definition per *role* (background, border, danger, ...) rather than one per
 *     shade, and dark mode is a single variable swap on `<html class="dark">`
 *     instead of a `dark:` variant on every element. Components ask for meaning
 *     ("bg-danger-subtle"), never for a hue.
 *
 * 2.  Colour is reserved for meaning. There are exactly four accent roles —
 *     success (recovered / allowed), warning (awaiting approval / caution),
 *     danger (failed / denied) and ai (model output). Anything that is not one
 *     of those four is neutral slate. An operator scanning this console under
 *     time pressure should be able to trust that a coloured pixel means
 *     something.
 *
 * No plugins: none are installed, and an offline `npm install` must succeed.
 */
import type { Config } from "tailwindcss";

const config = {
  // Class strategy rather than `media`: the console needs a user-controllable
  // toggle, and a media-only theme cannot be overridden by the operator.
  darkMode: "class",
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // --- Neutral surfaces -------------------------------------------------
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },

        // --- Semantic accents -------------------------------------------------
        // Each accent carries four tokens so a badge, a solid button and a tinted
        // panel can all be built from the same role without hand-picking shades:
        //   DEFAULT    solid fill / icon colour
        //   foreground text drawn on top of DEFAULT
        //   subtle     tinted surface behind quiet status chips
        //   strong     text colour that stays legible on `subtle`
        success: {
          DEFAULT: "hsl(var(--success))",
          foreground: "hsl(var(--success-foreground))",
          subtle: "hsl(var(--success-subtle))",
          strong: "hsl(var(--success-strong))",
        },
        warning: {
          DEFAULT: "hsl(var(--warning))",
          foreground: "hsl(var(--warning-foreground))",
          subtle: "hsl(var(--warning-subtle))",
          strong: "hsl(var(--warning-strong))",
        },
        danger: {
          DEFAULT: "hsl(var(--danger))",
          foreground: "hsl(var(--danger-foreground))",
          subtle: "hsl(var(--danger-subtle))",
          strong: "hsl(var(--danger-strong))",
        },
        // `ai` is its own role, not a reuse of `primary`: everything the model
        // produced is tinted with it, so a reviewer can see at a glance which
        // parts of a screen are machine-generated and which are recorded fact.
        ai: {
          DEFAULT: "hsl(var(--ai))",
          foreground: "hsl(var(--ai-foreground))",
          subtle: "hsl(var(--ai-subtle))",
          strong: "hsl(var(--ai-strong))",
        },
      },

      // shadcn-style radius scale driven by one variable, so the whole console's
      // "softness" is a single-line change rather than a find-and-replace.
      borderRadius: {
        xl: "calc(var(--radius) + 4px)",
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },

      fontFamily: {
        // System stack only. `next/font/google` would make `next build` reach out
        // to the network, and this project must build with no internet access.
        // 'Segoe UI' is listed early because the primary target machine is
        // Windows; ui-sans-serif/system-ui cover macOS and Linux ahead of it.
        sans: [
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "Noto Sans",
          "sans-serif",
          "Apple Color Emoji",
          "Segoe UI Emoji",
        ],
        // Headings share the sans stack but are tracked tighter in globals.css;
        // a second downloaded display face is not worth an offline-build failure.
        display: [
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        // Ids, hashes, amounts and JSON payloads. Cascadia Mono / Consolas are
        // the Windows entries; SFMono-Regular / Menlo cover macOS.
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Cascadia Mono",
          "Consolas",
          "Liberation Mono",
          "Courier New",
          "monospace",
        ],
      },

      fontSize: {
        // One extra step below `text-xs` for table meta-rows and hash strings,
        // where the default 12px is still too loud next to the primary value.
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },

      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(2px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        // Deliberately short and small: panels settle in rather than fly in.
        // Motion during an incident is noise, not delight.
        "fade-in": "fade-in 160ms ease-out",
      },
    },
  },
  plugins: [],
} satisfies Config;

export default config;
