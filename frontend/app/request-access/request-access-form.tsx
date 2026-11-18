// Client Component for the pending state and the inline result; the request
// itself is sent by the requestAccess Server Action.
"use client";

import { useActionState } from "react";

import { type RequestAccessState, requestAccess } from "@/lib/access-actions";

const initialState: RequestAccessState = { status: "idle" };

export function RequestAccessForm({ defaultEmail }: { defaultEmail: string }) {
  const [state, formAction, pending] = useActionState(requestAccess, initialState);

  if (state.status === "sent") {
    return (
      <p role="status" className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
        Thanks! Your request is in. You&apos;ll get an email if it&apos;s approved.
      </p>
    );
  }

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <label htmlFor="email" className="text-sm font-medium">
        Email
      </label>
      <input
        id="email"
        name="email"
        type="email"
        required
        autoComplete="email"
        defaultValue={defaultEmail}
        className="rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700"
      />
      <label htmlFor="message" className="text-sm font-medium">
        Message <span className="font-normal text-neutral-500">(optional)</span>
      </label>
      <textarea
        id="message"
        name="message"
        rows={4}
        maxLength={1000}
        placeholder="Who you are and what you'd like to try."
        className="rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700"
      />
      <button
        type="submit"
        disabled={pending}
        className="rounded-md bg-neutral-900 px-3 py-2 text-white disabled:opacity-60 dark:bg-white dark:text-neutral-900"
      >
        {pending ? "Sending…" : "Request access"}
      </button>
      {state.status === "error" && (
        <p role="alert" className="text-sm text-red-600">
          {state.message}
        </p>
      )}
    </form>
  );
}
