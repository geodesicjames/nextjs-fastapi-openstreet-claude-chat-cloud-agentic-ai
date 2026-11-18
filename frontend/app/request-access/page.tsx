import type { Metadata } from "next";
import { Suspense } from "react";

import { getCurrentUser } from "@/lib/api";

import { RequestAccessForm } from "./request-access-form";

export const metadata: Metadata = { title: "Request chat access · Places" };

export default function RequestAccessPage() {
  return (
    <main className="mx-auto flex w-full max-w-md flex-1 flex-col gap-6 px-6 py-16">
      <header>
        <h1 className="text-2xl font-semibold">Request chat access</h1>
        <p className="mt-2 text-neutral-600 dark:text-neutral-400">
          The map is open to everyone. The AI assistant is invite-only for now, because every
          message costs real money. Tell us a little about yourself and we&apos;ll email you if
          you&apos;re approved.
        </p>
      </header>
      {/* Prefilling the email needs the visitor's cookie, so that part renders
          per request; the fallback is the same form, just empty. */}
      <Suspense fallback={<RequestAccessForm defaultEmail="" />}>
        <PrefilledForm />
      </Suspense>
    </main>
  );
}

async function PrefilledForm() {
  const user = await getCurrentUser();
  return <RequestAccessForm defaultEmail={user?.email ?? ""} />;
}
