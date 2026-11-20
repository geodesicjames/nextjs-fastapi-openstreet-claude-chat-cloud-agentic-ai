// Admin page: review access requests and manage chat grants. Spend is on
// /admin/usage, agent runs and their traces on /admin/agent.
//
// The data is fetched server-side as the signed-in visitor. FastAPI's
// require_admin decides who sees it: for anyone else the admin endpoints
// return 401/403, and this page shows a message instead.
import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { getActiveGrants, getCurrentUser, getPendingAccessRequests } from "@/lib/api";

import { GrantRow } from "./grant-row";
import { RequestRow } from "./request-row";

export const metadata: Metadata = { title: "Admin · Places" };

export default function AdminPage() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-10 px-6 py-10">
      <div className="flex items-baseline justify-between gap-4">
        <h1 className="text-2xl font-semibold">Admin</h1>
        <nav className="flex gap-4 text-sm">
          <Link href="/admin/agent" className="underline underline-offset-4">
            Agent runs →
          </Link>
          <Link href="/admin/usage" className="underline underline-offset-4">
            Claude spend →
          </Link>
        </nav>
      </div>
      {/* Everything below depends on the visitor's cookie. */}
      <Suspense fallback={<p className="text-neutral-500">Loading…</p>}>
        <AdminContent />
      </Suspense>
    </main>
  );
}

async function AdminContent() {
  const [user, requests, grants] = await Promise.all([
    getCurrentUser(),
    getPendingAccessRequests(),
    getActiveGrants(),
  ]);

  if (!user) {
    return (
      <p>
        <Link href="/login" className="underline underline-offset-4">
          Sign in
        </Link>{" "}
        to see this page.
      </p>
    );
  }
  if (!user.is_admin || requests === null || grants === null) {
    return <p>This page is for admins only.</p>;
  }

  return (
    <>
      <section className="flex flex-col gap-4">
        <h2 className="text-lg font-semibold">
          Pending requests <span className="text-neutral-500">({requests.length})</span>
        </h2>
        {requests.length === 0 ? (
          <p className="text-neutral-500">No pending requests.</p>
        ) : (
          <ul className="flex flex-col gap-4">
            {requests.map((request) => (
              <RequestRow key={request.id} request={request} />
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-4">
        <h2 className="text-lg font-semibold">
          Active grants <span className="text-neutral-500">({grants.length})</span>
        </h2>
        <p className="text-sm text-neutral-500">
          Leave the limit empty to use the default. Usage resets at midnight UTC.
        </p>
        {grants.length === 0 ? (
          <p className="text-neutral-500">No active grants.</p>
        ) : (
          <ul className="flex flex-col gap-4">
            {grants.map((grant) => (
              <GrantRow key={grant.id} grant={grant} />
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
