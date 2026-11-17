// A Client Component: it needs interactivity (pending state and showing the
// result without a full page reload). The work itself still happens on the
// server, in the requestLoginLink Server Action.
"use client";

import { useActionState } from "react";

import { type LoginLinkState, requestLoginLink } from "@/lib/auth-actions";

const initialState: LoginLinkState = { status: "idle" };

export function LoginForm() {
  // useActionState calls the action with the form data and keeps whatever it
  // returns as `state`; `pending` is true while it runs.
  const [state, formAction, pending] = useActionState(requestLoginLink, initialState);

  if (state.status === "sent") {
    return (
      <p role="status" className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
        Check your email. If <strong>{state.email}</strong> can sign in, a link is on its way. It
        expires in 30 minutes.
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
        className="rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700"
      />
      <button
        type="submit"
        disabled={pending}
        className="rounded-md bg-neutral-900 px-3 py-2 text-white disabled:opacity-60 dark:bg-white dark:text-neutral-900"
      >
        {pending ? "Sending…" : "Email me a sign-in link"}
      </button>
      {state.status === "error" && (
        <p role="alert" className="text-sm text-red-600">
          {state.message}
        </p>
      )}
    </form>
  );
}
