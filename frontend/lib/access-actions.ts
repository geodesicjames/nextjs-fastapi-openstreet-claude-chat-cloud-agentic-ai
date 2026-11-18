// Server Actions for requesting chat access and for the admin page.
//
// Each admin action calls FastAPI as the signed-in visitor, so FastAPI's
// require_admin is what actually protects them; the /admin page hiding its
// buttons from non-admins is only a convenience.
"use server";

import { revalidatePath } from "next/cache";

import { apiFetch } from "@/lib/api";

export type RequestAccessState =
  { status: "idle" } | { status: "sent" } | { status: "error"; message: string };

/** Result of an admin action, shown next to the form that sent it. */
export type AdminActionState = { error: string | null };

const GENERIC_ERROR = "Something went wrong. Please try again.";

export async function requestAccess(
  _prev: RequestAccessState,
  formData: FormData,
): Promise<RequestAccessState> {
  const email = String(formData.get("email") ?? "").trim();
  const message = String(formData.get("message") ?? "").trim();
  try {
    const res = await apiFetch("/access-requests", {
      method: "POST",
      body: JSON.stringify({ email, message }),
    });
    if (res.status === 422) {
      return {
        status: "error",
        message: "Enter a valid email address (and keep the message under 1000 characters).",
      };
    }
    if (!res.ok) throw new Error(`access-requests failed: ${res.status}`);
  } catch (error) {
    console.error(error);
    return { status: "error", message: GENERIC_ERROR };
  }
  return { status: "sent" };
}

// --- Admin -------------------------------------------------------------------------

export async function approveRequest(
  _prev: AdminActionState,
  formData: FormData,
): Promise<AdminActionState> {
  const id = String(formData.get("id"));
  return adminCall(
    `/admin/access-requests/${encodeURIComponent(id)}/approve`,
    "POST",
    grantFields(formData),
  );
}

export async function denyRequest(
  _prev: AdminActionState,
  formData: FormData,
): Promise<AdminActionState> {
  const id = String(formData.get("id"));
  return adminCall(`/admin/access-requests/${encodeURIComponent(id)}/deny`, "POST");
}

export async function updateGrant(
  _prev: AdminActionState,
  formData: FormData,
): Promise<AdminActionState> {
  const id = String(formData.get("id"));
  // The edit form always shows every field, so send them all: an empty limit
  // means "use the default" and an empty date means "never expires".
  return adminCall(`/admin/grants/${encodeURIComponent(id)}`, "PATCH", grantFields(formData));
}

export async function revokeGrant(
  _prev: AdminActionState,
  formData: FormData,
): Promise<AdminActionState> {
  const id = String(formData.get("id"));
  return adminCall(`/admin/grants/${encodeURIComponent(id)}/revoke`, "POST");
}

/** Read the shared grant inputs: note, daily_budget_usd, expires_on. */
function grantFields(formData: FormData) {
  const budget = String(formData.get("daily_budget_usd") ?? "").trim();
  const expiresOn = String(formData.get("expires_on") ?? "").trim();
  return {
    note: String(formData.get("note") ?? "").trim(),
    // Not Number(budget) alone: Number("abc") is NaN, which JSON.stringify
    // turns into null and would silently mean "use the default". Sending the
    // raw text instead lets FastAPI reject it with a 422.
    daily_budget_usd:
      budget === "" ? null : Number.isFinite(Number(budget)) ? Number(budget) : budget,
    // A date input gives YYYY-MM-DD. Access lasts to the end of that day, UTC
    // (the same clock the daily budget uses).
    expires_at: expiresOn === "" ? null : `${expiresOn}T23:59:59Z`,
  };
}

async function adminCall(path: string, method: string, body?: object): Promise<AdminActionState> {
  let res: Response;
  try {
    res = await apiFetch(path, { method, body: body ? JSON.stringify(body) : undefined });
  } catch (error) {
    console.error(error);
    return { error: GENERIC_ERROR };
  }
  if (!res.ok) return { error: await errorMessage(res) };

  // Re-render /admin (and the header badge) with fresh data.
  revalidatePath("/admin");
  return { error: null };
}

/** A readable message from a FastAPI error response. */
async function errorMessage(res: Response): Promise<string> {
  if (res.status === 401 || res.status === 403) return "You need to be signed in as an admin.";
  try {
    const { detail } = (await res.json()) as { detail: unknown };
    // 409s carry a sentence; 422s carry a list of field errors.
    if (typeof detail === "string") return detail;
    if (res.status === 422) return "Check the values: budgets must be between $0.01 and $100.";
  } catch {
    // Not JSON; fall through.
  }
  return GENERIC_ERROR;
}
