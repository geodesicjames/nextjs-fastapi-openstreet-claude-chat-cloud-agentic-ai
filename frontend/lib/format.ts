// Display helpers shared by Server and Client Components.

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });

/** 12345 -> "12.3K" */
export function formatTokens(tokens: number): string {
  return compact.format(tokens);
}

// Fixed locale and UTC so the server and the browser render the same text
// (avoids hydration mismatches), and so dates match the backend's UTC days.
const dateFormat = new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeZone: "UTC" });

/** "2026-11-17T..." -> "17 Nov 2026" (UTC) */
export function formatDate(iso: string): string {
  return dateFormat.format(new Date(iso));
}

/** The UTC calendar date, for a date input's value: "2026-11-17". */
export function isoDate(iso: string): string {
  return new Date(iso).toISOString().slice(0, 10);
}
