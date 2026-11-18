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

/**
 * Read server-sent events from a fetch() response body, yielding each event
 * as soon as it arrives.
 *
 * The browser's EventSource only supports GET, so for a POST we read the
 * stream ourselves. Events are separated by a blank line; each has an
 * `event:` line and a `data:` line holding JSON.
 */
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<ChatEvent> {
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
      let type = "";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (type && data) yield { type, ...JSON.parse(data) } as ChatEvent;
    }
  }
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
