// One active grant: today's usage, an edit form, and Revoke.
"use client";

import { useActionState } from "react";

import { type AdminActionState, revokeGrant, updateGrant } from "@/lib/access-actions";
import type { Grant } from "@/lib/api";
import { formatDate, formatTokens, isoDate } from "@/lib/format";

import { ActionError, GrantFields, primaryButton, secondaryButton } from "./fields";

const initial: AdminActionState = { error: null };

export function GrantRow({ grant }: { grant: Grant }) {
  const [saveState, save, saving] = useActionState(updateGrant, initial);
  const [revokeState, revoke, revoking] = useActionState(revokeGrant, initial);
  const busy = saving || revoking;

  return (
    <li className="flex flex-col gap-3 rounded-lg border border-neutral-200 p-4 dark:border-neutral-800">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium">{grant.user_email}</span>
        <span className="text-sm text-neutral-500">
          {formatTokens(grant.tokens_used_today)} /{" "}
          {formatTokens(grant.effective_daily_token_limit)} tokens today
          {grant.daily_token_limit === null && " (default limit)"}
          {" · "}
          {grant.expires_at ? `expires ${formatDate(grant.expires_at)}` : "no expiry"}
        </span>
      </div>
      <form action={save} className="flex flex-col gap-3">
        <input type="hidden" name="id" value={grant.id} />
        <GrantFields
          idPrefix={`grant-${grant.id}`}
          note={grant.note}
          dailyTokenLimit={grant.daily_token_limit}
          expiresOn={grant.expires_at ? isoDate(grant.expires_at) : ""}
        />
        <div className="flex gap-2">
          <button type="submit" disabled={busy} className={primaryButton}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button
            type="submit"
            formAction={revoke}
            disabled={busy}
            // A native confirm is enough for an admin-only page.
            onClick={(event) => {
              if (!confirm(`Revoke chat access for ${grant.user_email}?`)) event.preventDefault();
            }}
            className={secondaryButton}
          >
            {revoking ? "Revoking…" : "Revoke"}
          </button>
        </div>
      </form>
      <ActionError error={saveState.error ?? revokeState.error} />
    </li>
  );
}
