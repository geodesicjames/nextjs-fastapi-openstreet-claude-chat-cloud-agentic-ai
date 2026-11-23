// Chat types (mirroring backend/app/schemas/chat.py and the SSE events in
// backend/app/services/chat.py) and a reader for the streamed reply.

import { categoryLabel } from "@/lib/map";

export type ChatAccessInfo = {
  allowed: boolean;
  reason: "no_grant" | "budget_exhausted" | null;
  daily_budget_usd: number;
  spent_today_usd: number;
  remaining_usd: number;
};

export type ConversationSummary = { id: string; title: string; updated_at: string };

export type StoredMessage = { id: string; role: "user" | "assistant"; text: string };

export type ChatPlace = {
  place_id: string;
  name: string | null;
  category: string;
  lat: number;
  lon: number;
};

/**
 * What the map shows as a message is sent (MapView in
 * backend/app/schemas/chat.py), so "near here" or a place name with no town
 * means the area on screen rather than anywhere in the world.
 */
export type MapView = {
  lat: number;
  lon: number;
  /** From the centre to the view's farthest corner. */
  radius_m: number;
  selected_place_id: string | null;
};

/** Great-circle distance in metres (haversine). */
export function distanceM(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a =
    Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6_371_000 * Math.asin(Math.sqrt(a));
}

export type ChatEvent =
  | { type: "text"; text: string }
  | { type: "tool_start"; tool: string; input: Record<string, unknown> }
  | { type: "tool_result"; tool: string; ok: boolean; summary: string; places: ChatPlace[] }
  | { type: "error"; code: string; message: string }
  | {
      type: "done";
      message_id: string | null;
      stop_reason: string | null;
      truncated: boolean;
      cost_usd: number;
    };

export type SseEvent = { event: string; data: unknown };

/**
 * Read server-sent events from a fetch() response body, yielding each event
 * as soon as it arrives.
 *
 * The browser's EventSource only supports GET and can't resume from a
 * position of our choosing, so we read the stream ourselves. Events are
 * separated by a blank line; each has an `event:` line and a `data:` line
 * holding JSON. Lines starting with ":" are comments (keep-alives).
 */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseEvent> {
  const reader = body.getReader();
  // stream: true keeps a multi-byte character that is split across chunks
  // (e.g. "é") intact until its second half arrives.
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end: number;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (event && data) yield { event, data: JSON.parse(data) };
    }
  }
}

/** The chat reply's events (see backend/app/services/chat.py). */
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<ChatEvent> {
  for await (const { event, data } of readSse(body)) {
    yield { type: event, ...(data as object) } as ChatEvent;
  }
}

// --- Access, shared by the Ask and Plan tabs ---------------------------------------

export type Access =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "signed_out" }
  | { kind: "ok"; info: ChatAccessInfo };

export async function fetchAccess(): Promise<Access> {
  try {
    const res = await fetch("/api/chat/access");
    if (res.status === 401) return { kind: "signed_out" };
    if (!res.ok) return { kind: "error" };
    return { kind: "ok", info: (await res.json()) as ChatAccessInfo };
  } catch {
    return { kind: "error" };
  }
}

/** A message for a failed chat or agent request, from its status or `detail`. */
export async function errorText(res: Response): Promise<string> {
  if (res.status === 401) return "Please sign in again.";
  if (res.status === 429) return "You've used today's chat budget. It resets at midnight UTC.";
  if (res.status === 503) return "The assistant isn't available right now.";
  try {
    const { detail } = (await res.json()) as { detail: unknown };
    if (typeof detail === "string") return detail;
  } catch {
    // not JSON
  }
  return "Something went wrong. Please try again.";
}

/** What the assistant is doing, in words, for a tool_start event. */
export function describeTool(tool: string, input: Record<string, unknown>): string {
  switch (tool) {
    case "find_location":
      return `Looking up “${String(input.query ?? "")}”`;
    case "search_places": {
      const categories = Array.isArray(input.categories) ? input.categories.map(String) : [];
      const what = categories.map((c) => categoryLabel(c).toLowerCase()).join(", ") || "places";
      return `Searching ${what} within ${String(input.radius_m ?? "?")} m`;
    }
    case "get_place_details":
      return "Checking place details";
    case "save_place":
      return "Saving the place";
    default:
      return `Using ${tool}`;
  }
}
