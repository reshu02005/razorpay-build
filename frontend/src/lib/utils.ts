/**
 * Small, dependency-light helpers shared across the console.
 *
 * Deliberately thin: anything that knows about the API belongs in `api.ts`,
 * anything that knows about display units belongs in `format.ts`. What is left
 * here is class-name composition and the one place a semantic `Tone` is turned
 * into Tailwind classes.
 */
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

import type { Tone } from "@/lib/types";

/**
 * Conditional class names, with later Tailwind utilities beating earlier ones.
 *
 * `clsx` handles the conditionals; `twMerge` resolves conflicts. Without the
 * merge step, `cn("px-3", "px-6")` would emit both and the winner would depend
 * on stylesheet order rather than on the caller's intent -- which makes a
 * component's `className` prop unable to override its own defaults.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * Tailwind classes for a quiet status chip in each semantic tone.
 *
 * Single source of truth on purpose. The tone maps in `@/lib/types`
 * (`RECOVERY_STATUS_TONE`, `GUARDRAIL_DECISION_TONE`, ...) decide *what* a value
 * means; this decides what that meaning looks like. If a screen picked its own
 * hue for "awaiting approval", the colour would stop being a reliable signal.
 *
 * `subtle` background + `strong` text rather than a solid fill: a table of forty
 * rows with forty saturated badges is unreadable, and this console is meant to
 * be scanned under pressure.
 */
export const TONE_BADGE_CLASSES: Record<Tone, string> = {
  success: "bg-success-subtle text-success-strong border-success/25",
  warning: "bg-warning-subtle text-warning-strong border-warning/25",
  danger: "bg-danger-subtle text-danger-strong border-danger/25",
  info: "bg-ai-subtle text-ai-strong border-ai/25",
  neutral: "bg-muted text-muted-foreground border-border",
};

/** Convenience accessor so callers can write `toneClasses(tone)` inline. */
export function toneClasses(tone: Tone): string {
  return TONE_BADGE_CLASSES[tone];
}

/**
 * Solid dot colour for the same tones, for timeline markers and status legends
 * where a full badge would be too heavy.
 */
export const TONE_DOT_CLASSES: Record<Tone, string> = {
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-ai",
  neutral: "bg-muted-foreground/50",
};

/**
 * Narrows an unknown thrown value to a message string.
 *
 * `catch` binds `unknown` under strict mode, and every screen needs the same
 * three-line narrowing to show an error. Doing it once here keeps `catch` blocks
 * from quietly casting to `any` to get at `.message`.
 */
export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return "Something went wrong.";
}
