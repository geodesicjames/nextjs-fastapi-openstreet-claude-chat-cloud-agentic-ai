// One pending request with Approve (plus optional grant settings) and Deny.
// A Client Component so each row can show its own pending state and error.
"use client";

import { useActionState } from "react";

import { type AdminActionState, approveRequest, denyRequest } from "@/lib/access-actions";
import type { AccessRequest } from "@/lib/api";
import { formatDate } from "@/lib/format";

import { ActionError, GrantFields, primaryButton, secondaryButton } from "./fields";

const initial: AdminActionState = { error: null };

export function RequestRow({ request }: { request: AccessRequest }) {
  const [approveState, approve, approving] = useActionState(approveRequest, initial);
  const [denyState, deny, denying] = useActionState(denyRequest, initial);
  const busy = approving || denying;

  return (
    <li className="flex flex-col gap-3 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium">{request.email}</span>
        <span className="text-sm text-neutral-500">{formatDate(request.created_at)}</span>
      </div>
      {request.message && (
        <p className="text-sm whitespace-pre-line text-neutral-700 dark:text-neutral-300">
          {request.message}
        </p>
      )}
      <form action={approve} className="flex flex-col gap-3">
        <input type="hidden" name="id" value={request.id} />
        <GrantFields idPrefix={`request-${request.id}`} />
        <div className="flex gap-2">
          <button type="submit" disabled={busy} className={primaryButton}>
            {approving ? "Approving…" : "Approve"}
          </button>
          {/* formAction sends this button's submit to a different action. */}
          <button type="submit" formAction={deny} disabled={busy} className={secondaryButton}>
            {denying ? "Denying…" : "Deny"}
          </button>
        </div>
      </form>
      <ActionError error={approveState.error ?? denyState.error} />
    </li>
  );
}
