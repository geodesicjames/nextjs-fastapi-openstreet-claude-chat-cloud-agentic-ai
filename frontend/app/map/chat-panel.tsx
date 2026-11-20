// The "Ask" tab: chat with Claude about places. Replies stream in as they
// are written; tool steps show as short status lines; places Claude finds
// are handed to the map (onPlaces) to show as markers.
"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  type Access,
  type ChatPlace,
  type ConversationSummary,
  describeTool,
  errorText,
  fetchAccess,
  readEvents,
  type StoredMessage,
} from "@/lib/chat";
import { formatUsd } from "@/lib/format";
import { Markdown } from "@/lib/markdown";

import { AccessNotice, canUseAssistant } from "./access-notice";

type ToolLine = { label: string; summary: string | null; ok: boolean };

type Message = {
  key: string;
  role: "user" | "assistant";
  text: string;
  tools: ToolLine[];
  error?: string;
};

const MAX_CHARS = 2000; // mirrors CHAT_MAX_MESSAGE_CHARS

export function ChatPanel({ onPlaces }: { onPlaces: (places: ChatPlace[]) => void }) {
  const router = useRouter();
  const [access, setAccess] = useState<Access>({ kind: "loading" });
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const refreshAccess = useCallback(() => fetchAccess().then(setAccess), []);
  const refreshConversations = useCallback(
    () => fetchConversations().then((list) => list && setConversations(list)),
    [],
  );

  useEffect(() => {
    // State is set in the promise callbacks, after the fetches resolve.
    void refreshAccess();
    void refreshConversations();
  }, [refreshAccess, refreshConversations]);

  // A block body, not `() => el.scrollIntoView()`: newer browsers return a
  // Promise from scroll methods, and an effect may only return a cleanup
  // function (React throws otherwise).
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  async function openConversation(id: string) {
    setProblem(null);
    if (!id) {
      setConversationId(null);
      setMessages([]);
      return;
    }
    const res = await fetch(`/api/chat/conversations/${encodeURIComponent(id)}`);
    if (!res.ok) return setProblem("Couldn't open that conversation.");
    const detail = (await res.json()) as { messages: StoredMessage[] };
    setConversationId(id);
    setMessages(detail.messages.map((m) => ({ key: m.id, role: m.role, text: m.text, tools: [] })));
  }

  // Apply a change to the assistant message being streamed (always the last).
  const updateLast = (change: (m: Message) => Message) =>
    setMessages((current) => [...current.slice(0, -1), change(current[current.length - 1])]);

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const text = String(new FormData(form).get("text") ?? "").trim();
    if (!text || pending) return;
    setPending(true);
    setProblem(null);

    let id = conversationId;
    if (!id) {
      const res = await fetch("/api/chat/conversations", { method: "POST" });
      if (!res.ok) {
        setPending(false);
        return setProblem(await errorText(res));
      }
      id = ((await res.json()) as { id: string }).id;
      setConversationId(id);
    }

    form.reset();
    const stamp = Date.now();
    setMessages((current) => [
      ...current,
      { key: `u${stamp}`, role: "user", text, tools: [] },
      { key: `a${stamp}`, role: "assistant", text: "", tools: [] },
    ]);

    try {
      const res = await fetch(`/api/chat/conversations/${encodeURIComponent(id)}/messages`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok || !res.body) {
        const message = await errorText(res);
        updateLast((m) => ({ ...m, error: message }));
        return;
      }
      for await (const e of readEvents(res.body)) {
        if (e.type === "text") updateLast((m) => ({ ...m, text: m.text + e.text }));
        else if (e.type === "tool_start")
          updateLast((m) => ({
            ...m,
            tools: [...m.tools, { label: describeTool(e.tool, e.input), summary: null, ok: true }],
          }));
        else if (e.type === "tool_result") {
          updateLast((m) => ({
            ...m,
            tools: m.tools.map((t, i) =>
              i === m.tools.length - 1 ? { ...t, summary: e.summary, ok: e.ok } : t,
            ),
          }));
          if (e.places.length) onPlaces(e.places);
        } else if (e.type === "error") updateLast((m) => ({ ...m, error: e.message }));
      }
    } catch (error) {
      console.error(error);
      updateLast((m) => ({ ...m, error: "The connection was interrupted. Please try again." }));
    } finally {
      setPending(false);
      void refreshAccess();
      void refreshConversations();
      router.refresh(); // update the header's budget badge
    }
  }

  if (!canUseAssistant(access)) {
    return <AccessNotice access={access} purpose="ask the assistant about places" />;
  }
  const outOfBudget = access.info.reason === "budget_exhausted";

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex items-center gap-2 text-sm">
        <label htmlFor="conversation" className="sr-only">
          Conversation
        </label>
        <select
          id="conversation"
          value={conversationId ?? ""}
          onChange={(e) => void openConversation(e.target.value)}
          className="min-w-0 flex-1 truncate rounded-md border border-neutral-300 bg-transparent px-2 py-1 dark:border-neutral-700"
        >
          <option value="">New conversation</option>
          {conversations.map((c) => (
            <option key={c.id} value={c.id}>
              {c.title}
            </option>
          ))}
        </select>
        <span className="shrink-0 text-xs text-neutral-500" title="Left of today's chat budget">
          {formatUsd(access.info.remaining_usd)} left
        </span>
      </div>

      <div className="flex flex-col gap-3 text-sm" aria-live="polite">
        {messages.length === 0 && (
          <p className="text-neutral-500">
            Ask about places, e.g. “Cafés near Leith Walk, Edinburgh?” or “Any parks with toilets
            near Bruntsfield?”. Places the assistant finds appear on the map.
          </p>
        )}
        {messages.map((m) =>
          m.role === "user" ? (
            <p
              key={m.key}
              className="self-end rounded-lg bg-neutral-100 px-3 py-2 whitespace-pre-wrap dark:bg-neutral-800"
            >
              {m.text}
            </p>
          ) : (
            <div key={m.key} className="flex flex-col gap-1.5">
              {m.tools.map((t, i) => (
                <p key={i} className="text-xs text-neutral-500">
                  {t.summary === null ? "⋯ " : t.ok ? "✓ " : "✕ "}
                  {t.label}
                  {t.summary ? ` · ${t.summary}` : ""}
                </p>
              ))}
              {m.text && <Markdown text={m.text} />}
              {m.error && (
                <p role="alert" className="text-red-600">
                  {m.error}
                </p>
              )}
              {pending &&
                m === messages[messages.length - 1] &&
                !m.text &&
                m.tools.length === 0 && <p className="text-neutral-500">Thinking…</p>}
            </div>
          ),
        )}
        <div ref={endRef} />
      </div>

      {problem && (
        <p role="alert" className="text-sm text-red-600">
          {problem}
        </p>
      )}
      {outOfBudget && (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          You&apos;ve used today&apos;s chat budget. It resets at midnight UTC.
        </p>
      )}

      <form onSubmit={send} className="flex flex-col gap-2">
        <label htmlFor="chat-text" className="sr-only">
          Message
        </label>
        <textarea
          id="chat-text"
          name="text"
          rows={2}
          maxLength={MAX_CHARS}
          required
          disabled={pending || outOfBudget}
          placeholder="Ask about places…"
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter adds a new line.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
          className="rounded-md border border-neutral-300 bg-transparent px-3 py-2 text-sm dark:border-neutral-700"
        />
        <button
          type="submit"
          disabled={pending || outOfBudget}
          className="self-end rounded-md bg-neutral-900 px-3 py-1.5 text-sm text-white disabled:opacity-60 dark:bg-white dark:text-neutral-900"
        >
          {pending ? "Answering…" : "Send"}
        </button>
      </form>
      <p className="text-xs text-neutral-500">
        Answers come from OpenStreetMap data and may be out of date.
      </p>
    </div>
  );
}

async function fetchConversations(): Promise<ConversationSummary[] | null> {
  const res = await fetch("/api/chat/conversations");
  return res.ok ? ((await res.json()) as ConversationSummary[]) : null;
}
