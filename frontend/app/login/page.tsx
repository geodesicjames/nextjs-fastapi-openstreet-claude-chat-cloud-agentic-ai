import type { Metadata } from "next";

import { LoginForm } from "./login-form";

export const metadata: Metadata = { title: "Sign in · Places" };

export default function LoginPage() {
  return (
    <main className="mx-auto flex w-full max-w-sm flex-1 flex-col gap-6 px-6 py-16">
      <header>
        <h1 className="text-2xl font-semibold">Sign in</h1>
        <p className="mt-2 text-neutral-600 dark:text-neutral-400">
          No password needed. We&apos;ll email you a link that signs you in.
        </p>
      </header>
      <LoginForm />
    </main>
  );
}
