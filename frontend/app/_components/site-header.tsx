// Site header with the sign-in state.
//
// The header itself is static. Only <UserMenu> depends on the visitor's
// cookie, so only it is wrapped in <Suspense> and rendered per request; the
// rest of every page can still be prerendered.
import Link from "next/link";
import { Suspense } from "react";

import { getCurrentUser } from "@/lib/api";
import { logout } from "@/lib/auth-actions";

export function SiteHeader() {
  return (
    <header className="border-b border-neutral-200 dark:border-neutral-800">
      <div className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-3">
        <Link href="/" className="font-semibold">
          Places
        </Link>
        <Suspense fallback={null}>
          <UserMenu />
        </Suspense>
      </div>
    </header>
  );
}

async function UserMenu() {
  const user = await getCurrentUser();

  if (!user) {
    return (
      <Link href="/login" className="text-sm underline-offset-4 hover:underline">
        Sign in
      </Link>
    );
  }

  return (
    <div className="flex items-center gap-4 text-sm">
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
