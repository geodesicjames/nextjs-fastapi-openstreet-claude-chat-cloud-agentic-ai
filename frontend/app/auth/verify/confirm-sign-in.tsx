"use client";

import Link from "next/link";
import { useActionState } from "react";

import { type VerifyState, verifyLogin } from "@/lib/auth-actions";

const initialState: VerifyState = { error: null };

export function ConfirmSignIn({ token }: { token: string }) {
  const [state, formAction, pending] = useActionState(verifyLogin, initialState);

  return (
    <form action={formAction} className="flex flex-col gap-3">
      <input type="hidden" name="token" value={token} />
      <button
        type="submit"
        disabled={pending}
        className="rounded-md bg-neutral-900 px-3 py-2 text-white disabled:opacity-60 dark:bg-white dark:text-neutral-900"
      >
        {pending ? "Signing in…" : "Sign in"}
      </button>
      {state.error && (
        <p role="alert" className="text-sm text-red-600">
          {state.error}{" "}
          <Link href="/login" className="underline">
            Get a new link
          </Link>
        </p>
      )}
    </form>
  );
}
