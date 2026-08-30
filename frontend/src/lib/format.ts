/**
 * Display formatters shared by every screen.
 *
 * Two rules this module exists to enforce:
 *
 * 1.  **No component ever divides by 100.** Money crosses the wire as integer
 *     paise (the value of record) alongside a float rupee field. Components pass
 *     paise to `formatRupees` and get a string back. Scattering `/ 100` through
 *     JSX is how a currency bug gets into a demo.
 *
 * 2.  **Nothing here depends on the viewer's locale or clock.** Next.js renders
 *     these strings on the server first and then again in the browser during
 *     hydration. If a formatter inherited the ambient locale or timezone, the two
 *     passes would disagree and React would throw a hydration mismatch -- so
 *     locale and timezone are pinned explicitly below.
 *
 * Everything is a pure function. No date library is installed and none is
 * needed: the whole surface is six formatters over `Intl` and arithmetic.
 */

/**
 * Fixed display locale. Indian digit grouping is the reason this is explicit
 * rather than inherited from the browser: `en-IN` groups by lakh and crore
 * (12,34,567) while `en-US` groups by thousand (1,234,567). A merchant console
 * denominated in INR that renders Western grouping looks wrong to the only
 * people who will use it, and `navigator.language` would make the same amount
 * render differently for two reviewers.
 */
const LOCALE = "en-IN";

/**
 * Fixed display timezone. The alternative -- letting each runtime use its own --
 * means the server (usually UTC) and the browser (usually IST) format the same
 * timestamp differently, which surfaces as a hydration mismatch on every screen
 * that shows a date. Pinning IST also matches the operators this console is for.
 */
const TIME_ZONE = "Asia/Kolkata";

/** Rendered in place of a value that is missing or not a finite number. */
const EMPTY = "—"; // em dash

const rupeeFormatter = new Intl.NumberFormat(LOCALE, {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

const dateTimeFormatter = new Intl.DateTimeFormat(LOCALE, {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  // 24-hour: this is an operations console, and "14:05" is unambiguous in a
  // column of timestamps in a way that "2:05 pm" is not.
  hour12: false,
  timeZone: TIME_ZONE,
  timeZoneName: "short",
});

/**
 * Formats integer paise as an INR amount, e.g. `123456` -> `"₹1,234.56"`.
 *
 * Paise is the unit of record everywhere upstream of the API edge, so this is
 * the single place the conversion to a human-readable amount happens.
 */
export function formatRupees(paise: number): string {
  if (!Number.isFinite(paise)) return EMPTY;
  return rupeeFormatter.format(paise / 100);
}

/**
 * Compact INR for KPI tiles, e.g. `"₹12.3L"`, `"₹4.2Cr"`.
 *
 * The thresholds are hand-rolled rather than delegated to
 * `Intl.NumberFormat(..., { notation: "compact" })` on purpose: the compact
 * notation for `en-IN` depends on the ICU data bundled with the runtime, so the
 * same number can come out as "12L" in one Node build and "1.2M" in another.
 * A KPI that renders differently on the server and in the browser is a hydration
 * mismatch; a KPI that renders differently on two reviewers' machines is worse.
 */
export function formatCompactRupees(paise: number): string {
  if (!Number.isFinite(paise)) return EMPTY;

  const rupees = paise / 100;
  const sign = rupees < 0 ? "-" : "";
  const abs = Math.abs(rupees);

  const scale = (value: number, suffix: string): string => {
    // One decimal below 100 units, none above: "₹1.2Cr" but "₹142Cr".
    const digits = value < 100 ? 1 : 0;
    const shown = value.toFixed(digits).replace(/\.0$/, "");
    return `${sign}₹${shown}${suffix}`;
  };

  if (abs >= 1_00_00_000) return scale(abs / 1_00_00_000, "Cr");
  if (abs >= 1_00_000) return scale(abs / 1_00_000, "L");
  if (abs >= 1_000) return scale(abs / 1_000, "K");
  return `${sign}₹${abs.toFixed(abs % 1 === 0 ? 0 : 2)}`;
}

/**
 * Formats a value that is *already* a percentage (0..100), e.g. the
 * `recovery_rate_pct` field: `62.5` -> `"62.5%"`.
 *
 * Fractions in the 0..1 range (confidence, propensity) go through
 * `formatConfidence` instead. Keeping the two apart means a caller never has to
 * guess whether a formatter is going to multiply by 100 for them.
 */
export function formatPercent(pct: number, fractionDigits = 1): string {
  if (!Number.isFinite(pct)) return EMPTY;
  return `${pct.toFixed(fractionDigits)}%`;
}

/**
 * Formats a 0..1 score as a whole percentage, e.g. `0.87` -> `"87%"`.
 *
 * Used for classification confidence and propensity. No decimal place: a model
 * score is not precise enough to justify one, and "86.7%" invites an operator to
 * read significance that is not there.
 */
export function formatConfidence(score: number): string {
  if (!Number.isFinite(score)) return EMPTY;
  const clamped = Math.min(Math.max(score, 0), 1);
  return `${Math.round(clamped * 100)}%`;
}

/** Accepts what the API sends (ISO-8601 strings) or an already-parsed Date. */
export type DateInput = string | number | Date | null | undefined;

function toDate(input: DateInput): Date | null {
  if (input === null || input === undefined) return null;
  const date = input instanceof Date ? input : new Date(input);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * Absolute timestamp, e.g. `"30 Aug 2026, 14:05 GMT+5:30"`.
 *
 * Safe in a server component: locale and timezone are pinned, so the server and
 * client passes produce byte-identical output.
 */
export function formatDateTime(input: DateInput): string {
  const date = toDate(input);
  if (date === null) return EMPTY;
  return dateTimeFormatter.format(date);
}

/**
 * Terse relative time, e.g. `"just now"`, `"4m ago"`, `"3d ago"`, `"in 12m"`.
 *
 * IMPORTANT: this reads the wall clock, so its output differs between the server
 * render and the hydration pass. Call it only from client components (or from a
 * component that has already mounted). `now` is injectable so callers can pass a
 * stable reference point and so the function stays testable.
 *
 * Falls back to an absolute date beyond a week: "37d ago" is a number an
 * operator has to decode, whereas a date is immediately meaningful.
 */
export function formatRelativeTime(input: DateInput, now: DateInput = new Date()): string {
  const date = toDate(input);
  const reference = toDate(now);
  if (date === null || reference === null) return EMPTY;

  const deltaMs = date.getTime() - reference.getTime();
  const future = deltaMs > 0;
  const seconds = Math.abs(deltaMs) / 1000;

  if (seconds < 45) return "just now";

  const render = (value: number, unit: string): string => {
    const rounded = Math.round(value);
    return future ? `in ${rounded}${unit}` : `${rounded}${unit} ago`;
  };

  if (seconds < 3600) return render(seconds / 60, "m");
  if (seconds < 86_400) return render(seconds / 3600, "h");
  if (seconds < 604_800) return render(seconds / 86_400, "d");
  return formatDateTime(date);
}

/**
 * Shortens an opaque identifier for display, e.g.
 * `"pay_QkLm1n2o3p4q5r"` -> `"pay_QkLm…5r"`.
 *
 * Ids appear in dense tables where the full string would dominate the row, but
 * the head and tail are what a human matches against a Razorpay dashboard, so
 * both ends are kept. Anything already short enough is returned untouched.
 */
export function truncateId(id: string, head = 10, tail = 4): string {
  if (!id) return EMPTY;
  if (id.length <= head + tail + 1) return id;
  return `${id.slice(0, head)}…${id.slice(-tail)}`;
}
