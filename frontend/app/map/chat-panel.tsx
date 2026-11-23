// The "Ask" tab: chat with Claude about places. Replies stream in as they
// are written; tool steps show as short status lines; places Claude finds
// are handed to the map (onPlaces) to show as markers.
//
// Conversations are a stack of rows, one per conversation, like an
// accordion: clicking a row opens that conversation (and its message box)
// beneath it, and only one is open at a time. The top row is a new
// conversation, open whenever no saved one is.
//
// A **bold** name in an answer that matches a place found while writing it
// is a link: it shows the place on the map and starts a new conversation
// about it, with the question typed in but not sent (sending costs money).
"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import {
  type Access,
  type ChatPlace,
  type ConversationSummary,
  describeTool,
  errorText,
  fetchAccess,
  type MapView,
  nameKey,
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
  /** For an answer: places found while writing it, for linking bold names. */
  places: ChatPlace[];
  error?: string;
  /** A saved question's id (for deleting it); unset until the reply is saved. */
  id?: string;
};

/** A question and the replies to it, shown and folded together. */
type Turn = { question: Message; replies: Message[] };

function toTurns(messages: Message[]): Turn[] {
  const turns: Turn[] = [];
  for (const m of messages) {
    if (m.role === "user") turns.push({ question: m, replies: [] });
    else turns[turns.length - 1]?.replies.push(m);
  }
  return turns;
}

const MAX_CHARS = 2000; // mirrors CHAT_MAX_MESSAGE_CHARS

export function ChatPanel({
  onPlaces,
  onShowPlace,
  getMapView,
}: {
  onPlaces: (places: ChatPlace[]) => void;
  /** Show one place on the map (a linked name was clicked). */
  onShowPlace: (place: ChatPlace) => void;
  /** Read when a message is sent; null before the map has loaded. */
  getMapView: () => MapView | null;
}) {
  const router = useRouter();
  const [access, setAccess] = useState<Access>({ kind: "loading" });
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // Questions folded to one line (by message key), so a long chat stays short.
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [draft, setDraft] = useState("");
  const [focusTick, setFocusTick] = useState(0);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

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

  // After a linked name is clicked; the message box may have moved rows.
  useEffect(() => {
    if (focusTick) inputRef.current?.focus();
  }, [focusTick]);

  /** Open a saved conversation, or (null) a new one. */
  async function openConversation(id: string | null) {
    setProblem(null);
    if (!id) {
      setConversationId(null);
      setMessages([]);
      setFolded(new Set());
      return;
    }
    const res = await fetch(`/api/chat/conversations/${encodeURIComponent(id)}`);
    if (!res.ok) return setProblem("Couldn't open that conversation.");
    const detail = (await res.json()) as { messages: StoredMessage[] };
    const loaded: Message[] = detail.messages.map((m) => ({
      key: m.id,
      role: m.role,
      text: m.text,
      tools: [],
      places: m.places,
      id: m.role === "user" ? m.id : undefined,
    }));
    setConversationId(id);
    setMessages(loaded);
    // Show only the latest question's answer; fold the rest.
    const questions = loaded.filter((m) => m.role === "user").map((m) => m.key);
    setFolded(new Set(questions.slice(0, -1)));
  }

  function toggleFold(key: string) {
    setFolded((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  }

  async function deleteTurn(turn: Turn) {
    const questionId = turn.question.id;
    if (!conversationId || !questionId) return;
    if (!window.confirm("Delete this question and its answer? The assistant will forget them."))
      return;
    setProblem(null);
    const res = await fetch(
      `/api/chat/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(questionId)}`,
      { method: "DELETE" },
    );
    if (!res.ok) return setProblem(await errorText(res));
    const gone = new Set([turn.question.key, ...turn.replies.map((m) => m.key)]);
    const left = messages.filter((m) => !gone.has(m.key));
    if (left.some((m) => m.role === "user")) setMessages(left);
    else void openConversation(null); // that was the last question: the conversation is gone
    void refreshConversations(); // the title may have been that question
  }

  async function deleteConversation(conversation: ConversationSummary) {
    if (!window.confirm(`Delete “${conversation.title}” and everything in it?`)) return;
    setProblem(null);
    const res = await fetch(`/api/chat/conversations/${encodeURIComponent(conversation.id)}`, {
      method: "DELETE",
    });
    if (!res.ok) return setProblem(await errorText(res));
    if (conversation.id === conversationId) void openConversation(null);
    setConversations((current) => current.filter((c) => c.id !== conversation.id));
  }

  /** A linked name was clicked: show the place and start asking about it. */
  function askAbout(place: ChatPlace) {
    onShowPlace(place);
    void openConversation(null);
    setDraft(`Tell me more about ${place.name}.`);
    setFocusTick((n) => n + 1);
  }

  /** Bold text in an answer: a link if it names one of the answer's places. */
  function placeLinks(places: ChatPlace[]) {
    if (!places.length) return undefined;
    const byName = new Map(places.filter((p) => p.name).map((p) => [nameKey(p.name!), p]));
    return function PlaceLink(text: string) {
      const place = byName.get(nameKey(text));
      if (!place) return null;
      return (
        <button
          type="button"
          onClick={() => askAbout(place)}
          disabled={pending}
          title={`Show ${place.name} on the map and ask about it`}
          className="font-semibold underline decoration-dotted underline-offset-2 hover:text-blue-700 disabled:no-underline dark:hover:text-blue-400"
        >
          {text}
        </button>
      );
    };
  }

  // Apply a change to the assistant message being streamed (always the last).
  const updateLast = (change: (m: Message) => Message) =>
    setMessages((current) => [...current.slice(0, -1), change(current[current.length - 1])]);

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = draft.trim();
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

    setDraft("");
    const stamp = Date.now();
    const questionKey = `u${stamp}`;
    // Fold the earlier questions, so the new answer is what's on screen.
    setFolded(new Set(messages.filter((m) => m.role === "user").map((m) => m.key)));
    setMessages((current) => [
      ...current,
      { key: questionKey, role: "user", text, tools: [], places: [] },
      { key: `a${stamp}`, role: "assistant", text: "", tools: [], places: [] },
    ]);

    try {
      const res = await fetch(`/api/chat/conversations/${encodeURIComponent(id)}/messages`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text, map: getMapView() }),
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
            places: [...m.places, ...e.places],
          }));
          if (e.places.length) onPlaces(e.places);
        } else if (e.type === "error") updateLast((m) => ({ ...m, error: e.message }));
        else if (e.type === "done" && e.user_message_id) {
          const questionId = e.user_message_id;
          setMessages((current) =>
            current.map((m) => (m.key === questionKey ? { ...m, id: questionId } : m)),
          );
        }
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

  // The open conversation: its questions and answers, then the message box.
  const openBody = (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex flex-col gap-3 text-sm" aria-live="polite">
        {messages.length === 0 && (
          <p className="text-neutral-500">
            Ask about places, e.g. “Cafés near Leith Walk, Edinburgh?” or “Any parks with toilets
            near Bruntsfield?”. Places the assistant finds appear on the map.
          </p>
        )}
        {toTurns(messages).map((turn) => {
          const { question } = turn;
          const isFolded = folded.has(question.key);
          return (
            <div key={question.key} className="flex flex-col gap-2">
              <div className="flex items-start gap-1.5 self-end">
                <button
                  type="button"
                  onClick={() => toggleFold(question.key)}
                  aria-expanded={!isFolded}
                  aria-label={isFolded ? "Show answer" : "Hide answer"}
                  title={isFolded ? "Show answer" : "Hide answer"}
                  className="mt-2 shrink-0 text-xs text-neutral-500 hover:text-neutral-900 dark:hover:text-white"
                >
                  {isFolded ? "▸" : "▾"}
                </button>
                <p
                  className={`rounded-lg bg-neutral-100 px-3 py-2 dark:bg-neutral-800 ${
                    isFolded ? "line-clamp-1 break-all" : "whitespace-pre-wrap"
                  }`}
                >
                  {question.text}
                </p>
                <button
                  type="button"
                  onClick={() => void deleteTurn(turn)}
                  disabled={!question.id || pending}
                  aria-label="Delete question and answer"
                  title="Delete question and answer"
                  className="mt-1.5 shrink-0 rounded px-1 text-neutral-400 hover:text-red-600 disabled:invisible"
                >
                  ×
                </button>
              </div>
              {!isFolded &&
                turn.replies.map((m) => (
                  <div key={m.key} className="flex flex-col gap-1.5">
                    {m.tools.map((t, i) => (
                      <p key={i} className="text-xs text-neutral-500">
                        {t.summary === null ? "⋯ " : t.ok ? "✓ " : "✕ "}
                        {t.label}
                        {t.summary ? ` · ${t.summary}` : ""}
                      </p>
                    ))}
                    {m.text && <Markdown text={m.text} renderStrong={placeLinks(m.places)} />}
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
                ))}
            </div>
          );
        })}
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
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
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
    </div>
  );

  // A conversation that was just started isn't in the list until its first
  // reply is saved; until then it stays in the top row.
  const openIsListed = conversations.some((c) => c.id === conversationId);
  // An open row doesn't repeat its title: that's the first question, shown
  // just below it. It says how many questions there are instead.
  const questionCount = messages.filter((m) => m.role === "user").length;
  const openLabel = questionCount
    ? `${questionCount} question${questionCount === 1 ? "" : "s"}`
    : "New conversation";

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <p className="self-end text-xs text-neutral-500" title="Left of today's chat budget">
        {formatUsd(access.info.remaining_usd)} left
      </p>

      <ul
        aria-label="Conversations"
        className="flex flex-col divide-y divide-neutral-300 rounded-md border border-neutral-300 dark:divide-neutral-700 dark:border-neutral-700"
      >
        <ConversationRow
          title="New conversation"
          label={openIsListed ? "+ New conversation" : openLabel}
          open={!openIsListed}
          disabled={pending}
          onToggle={() => void openConversation(null)}
        >
          {!openIsListed && openBody}
        </ConversationRow>
        {conversations.map((c) => {
          const open = c.id === conversationId;
          return (
            <ConversationRow
              key={c.id}
              title={c.title}
              label={open ? openLabel : c.title}
              open={open}
              disabled={pending}
              onToggle={() => void openConversation(open ? null : c.id)}
              onDelete={() => void deleteConversation(c)}
            >
              {open && openBody}
            </ConversationRow>
          );
        })}
      </ul>

      <p className="text-xs text-neutral-500">
        Answers come from OpenStreetMap data and may be out of date.
      </p>
    </div>
  );
}

/** One row of the conversation stack: a button that opens or closes it, an
 * optional delete button, and (when open) the conversation beneath. */
function ConversationRow({
  title,
  label,
  open,
  disabled,
  onToggle,
  onDelete,
  children,
}: {
  /** The conversation's title (for the delete button and tooltip). */
  title: string;
  /** What the row shows. */
  label: string;
  open: boolean;
  disabled: boolean;
  onToggle: () => void;
  onDelete?: () => void;
  children: ReactNode;
}) {
  return (
    <li>
      <div
        className={`flex items-center text-sm ${open ? "bg-neutral-100 dark:bg-neutral-800" : ""}`}
      >
        <button
          type="button"
          onClick={onToggle}
          disabled={disabled}
          aria-expanded={open}
          title={title}
          className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left hover:bg-neutral-100 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:bg-neutral-800"
        >
          <span aria-hidden className="shrink-0 text-xs text-neutral-500">
            {open ? "▾" : "▸"}
          </span>
          <span className={`truncate ${open ? "font-medium" : ""}`}>{label}</span>
        </button>
        {onDelete && (
          <button
            type="button"
            onClick={onDelete}
            disabled={disabled}
            aria-label={`Delete conversation: ${title}`}
            title="Delete conversation"
            className="shrink-0 self-stretch px-3 text-neutral-400 hover:text-red-600 disabled:invisible"
          >
            ×
          </button>
        )}
      </div>
      {children}
    </li>
  );
}

async function fetchConversations(): Promise<ConversationSummary[] | null> {
  const res = await fetch("/api/chat/conversations");
  return res.ok ? ((await res.json()) as ConversationSummary[]) : null;
}
