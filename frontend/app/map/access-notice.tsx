// What the Ask and Plan tabs show instead of their controls when the
// visitor can't use the assistant: still loading, signed out, or not invited.
import Link from "next/link";

import type { Access } from "@/lib/chat";

/** True when the visitor may use the assistant (they may still be out of budget). */
export function canUseAssistant(access: Access): access is Extract<Access, { kind: "ok" }> {
  return access.kind === "ok" && access.info.reason !== "no_grant";
}

export function AccessNotice({ access, purpose }: { access: Access; purpose: string }) {
  if (access.kind === "loading") return <p className="text-sm text-neutral-500">Loading…</p>;
  if (access.kind === "error") {
    return <p className="text-sm text-red-600">The assistant isn&apos;t available right now.</p>;
  }
  if (access.kind === "signed_out") {
    return (
      <p className="text-sm">
        <Link href="/login" className="underline underline-offset-4">
          Sign in
        </Link>{" "}
        to {purpose}.
      </p>
    );
  }
  return (
    <p className="text-sm">
      The assistant is invite-only for now.{" "}
      <Link href="/request-access" className="underline underline-offset-4">
        Request access
      </Link>
      .
    </p>
  );
}
