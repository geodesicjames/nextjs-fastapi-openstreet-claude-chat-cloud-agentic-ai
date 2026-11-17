// Server Actions for signing in and out.
//
// A Server Action is an async function that runs on the server but can be
// passed straight to a <form action={...}>, roughly a Django view that only
// handles POST. Next.js wires up the request, and checks the Origin header so
// other sites cannot submit these forms (CSRF protection).
"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { apiFetch } from "@/lib/api";

export type LoginLinkState =
  { status: "idle" } | { status: "sent"; email: string } | { status: "error"; message: string };

export type VerifyState = { error: string | null };

export async function requestLoginLink(
  _prev: LoginLinkState,
  formData: FormData,
): Promise<LoginLinkState> {
  const email = String(formData.get("email") ?? "").trim();
  try {
    const res = await apiFetch("/auth/login-link", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
    if (res.status === 422) return { status: "error", message: "Enter a valid email address." };
    if (!res.ok) throw new Error(`login-link failed: ${res.status}`);
  } catch (error) {
    console.error(error);
    return { status: "error", message: "Something went wrong. Please try again." };
  }
  return { status: "sent", email };
}

export async function verifyLogin(_prev: VerifyState, formData: FormData): Promise<VerifyState> {
  const token = String(formData.get("token") ?? "");
  let res: Response;
  try {
    res = await apiFetch("/auth/verify", { method: "POST", body: JSON.stringify({ token }) });
  } catch (error) {
    console.error(error);
    return { error: "Something went wrong. Please try again." };
  }
  if (!res.ok) {
    return { error: "This sign-in link is invalid or has expired. Request a new one." };
  }

  await relaySetCookies(res);
  // redirect() works by throwing, so it must stay outside the try/catch.
  redirect("/");
}

export async function logout(): Promise<void> {
  const res = await apiFetch("/auth/logout", { method: "POST" });
  await relaySetCookies(res);
  redirect("/");
}

/**
 * Pass cookies set by FastAPI on to the browser.
 *
 * FastAPI answered *this server*, not the browser, so its Set-Cookie headers
 * would otherwise be lost. Re-setting them here puts the session cookie on
 * the Next.js origin, with the attributes (HttpOnly, SameSite, Max-Age...)
 * that FastAPI chose.
 */
async function relaySetCookies(res: Response): Promise<void> {
  const cookieStore = await cookies();
  for (const header of res.headers.getSetCookie()) {
    const [pair, ...attributes] = header.split(";").map((part) => part.trim());
    const eq = pair.indexOf("=");
    const name = pair.slice(0, eq);
    const value = pair.slice(eq + 1).replace(/^"(.*)"$/, "$1");

    const options: Parameters<typeof cookieStore.set>[2] = {};
    for (const attribute of attributes) {
      const [rawKey, ...rest] = attribute.split("=");
      const key = rawKey.toLowerCase();
      const attrValue = rest.join("=");
      if (key === "max-age") options.maxAge = Number(attrValue);
      else if (key === "expires") options.expires = new Date(attrValue);
      else if (key === "path") options.path = attrValue;
      else if (key === "httponly") options.httpOnly = true;
      else if (key === "secure") options.secure = true;
      else if (key === "samesite")
        options.sameSite = attrValue.toLowerCase() as "lax" | "strict" | "none";
    }
    cookieStore.set(name, value, options);
  }
}
