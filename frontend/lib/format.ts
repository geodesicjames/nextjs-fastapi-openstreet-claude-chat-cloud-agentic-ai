// Display helpers shared by Server and Client Components.

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

/** 0.4567 -> "$0.46"; tiny non-zero amounts -> "<$0.01" rather than "$0.00". */
export function formatUsd(amount: number): string {
  return amount > 0 && amount < 0.005 ? "<$0.01" : usd.format(amount);
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
