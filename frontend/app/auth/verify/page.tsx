// The page a magic link opens.
//
// It does not sign the user in by itself: they have to press the button,
// which POSTs the token. Email security scanners often open links ahead of
// the user; if a plain GET signed people in, the scanner would use up the
// single-use token first.
import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { ConfirmSignIn } from "./confirm-sign-in";

export const metadata: Metadata = { title: "Confirm sign-in · Places" };

export default function VerifyPage({ searchParams }: PageProps<"/auth/verify">) {
  return (
    <main className="mx-auto flex w-full max-w-sm flex-1 flex-col gap-6 px-6 py-16">
      <h1 className="text-2xl font-semibold">Confirm sign-in</h1>
      {/* searchParams are only known per request, so reading them happens
          inside a Suspense boundary (required with Cache Components). */}
      <Suspense fallback={null}>
        <TokenForm searchParams={searchParams} />
      </Suspense>
    </main>
  );
}

async function TokenForm({ searchParams }: Pick<PageProps<"/auth/verify">, "searchParams">) {
  const { token } = await searchParams;

  if (typeof token !== "string" || token === "") {
    return (
      <p>
        This link is incomplete.{" "}
        <Link href="/login" className="underline">
          Request a new sign-in link
        </Link>
        .
      </p>
    );
  }

  return <ConfirmSignIn token={token} />;
}
