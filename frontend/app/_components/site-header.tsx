// Site header with the sign-in state.
//
// The header itself is static. Only <UserMenu> depends on the visitor's
// cookie, so only it is wrapped in <Suspense> and rendered per request; the
// rest of every page can still be prerendered.
import Link from "next/link";
import { Suspense } from "react";

import { type ChatAccess, getChatAccess, getCurrentUser } from "@/lib/api";
import { logout } from "@/lib/auth-actions";
import { formatUsd } from "@/lib/format";

export function SiteHeader() {
  return (
    <header className="border-b border-neutral-200 dark:border-neutral-800">
      <div className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-3">
        <nav className="flex items-center gap-5">
          <Link href="/" className="font-semibold">
            Places
          </Link>
          <Link href="/map" className="text-sm underline-offset-4 hover:underline">
            Map
          </Link>
        </nav>
        <Suspense fallback={null}>
          <UserMenu />
        </Suspense>
      </div>
    </header>
  );
}

async function UserMenu() {
  // Both requests start at once rather than one after the other.
  const [user, chat] = await Promise.all([getCurrentUser(), getChatAccess()]);

  if (!user) {
    return (
      <Link href="/login" className="text-sm underline-offset-4 hover:underline">
        Sign in
      </Link>
    );
  }

  return (
    <div className="flex items-center gap-4 text-sm">
      {chat && <ChatBadge chat={chat} />}
      {user.is_admin && (
        <Link href="/admin" className="underline-offset-4 hover:underline">
          Admin
        </Link>
      )}
      <span className="text-neutral-600 dark:text-neutral-400">{user.email}</span>
      {/* A plain form posting to a Server Action: works even before any
          JavaScript has loaded. */}
      <form action={logout}>
        <button type="submit" className="underline-offset-4 hover:underline">
          Sign out
        </button>
      </form>
    </div>
  );
}

/** Chat availability: budget left today, or why chat is unavailable. */
function ChatBadge({ chat }: { chat: ChatAccess }) {
  if (chat.reason === "no_grant") {
    return (
      <Link href="/request-access" className="underline-offset-4 hover:underline">
        Request chat access
      </Link>
    );
  }

  const exhausted = chat.reason === "budget_exhausted";
  return (
    <span
      title={`Spent ${formatUsd(chat.spent_today_usd)} of ${formatUsd(chat.daily_budget_usd)} today. Resets at midnight UTC.`}
      className={`rounded-full border px-2 py-0.5 ${
        exhausted
          ? "border-amber-400 text-amber-700 dark:text-amber-400"
          : "border-neutral-300 text-neutral-600 dark:border-neutral-700 dark:text-neutral-400"
      }`}
    >
      {exhausted ? "Chat limit reached today" : `Chat: ${formatUsd(chat.remaining_usd)} left today`}
    </span>
  );
}
