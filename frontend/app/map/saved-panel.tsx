// The "Saved" tab: places the assistant saved for the signed-in user.
"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { categoryLabel } from "@/lib/map";

type SavedPlace = {
  id: string;
  place_id: string;
  name: string | null;
  category: string;
  lat: number;
  lon: number;
  note: string;
};

export function SavedPanel({ onShow }: { onShow: (place: SavedPlace) => void }) {
  const [saved, setSaved] = useState<SavedPlace[] | "signed_out" | null>(null);

  const load = useCallback(() => fetchSaved().then(setSaved), []);

  useEffect(() => {
    // State is set in the promise callback, after the fetch resolves.
    void load();
  }, [load]);

  async function remove(id: string) {
    const res = await fetch(`/api/saved-places/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (res.ok) void load();
  }

  if (saved === null) return <p className="text-sm text-neutral-500">Loading…</p>;
  if (saved === "signed_out") {
    return (
      <p className="text-sm">
        <Link href="/login" className="underline underline-offset-4">
          Sign in
        </Link>{" "}
        to see your saved places.
      </p>
    );
  }
  if (saved.length === 0) {
    return (
      <p className="text-sm text-neutral-500">
        Nothing saved yet. Ask the assistant to save a place, e.g. “Save Bean There”.
      </p>
    );
  }
  return (
    <ul className="flex flex-col divide-y divide-neutral-200 text-sm dark:divide-neutral-800">
      {saved.map((s) => (
        <li key={s.id} className="flex items-start justify-between gap-2 py-2">
          <button type="button" onClick={() => onShow(s)} className="text-left hover:underline">
            <span className="font-medium">{s.name ?? `Unnamed ${categoryLabel(s.category)}`}</span>
            <span className="block text-xs text-neutral-500">{categoryLabel(s.category)}</span>
            {s.note && (
              <span className="block text-neutral-600 dark:text-neutral-400">{s.note}</span>
            )}
          </button>
          <button
            type="button"
            onClick={() => void remove(s.id)}
            aria-label={`Remove ${s.name ?? "place"}`}
            className="text-neutral-500"
          >
            ✕
          </button>
        </li>
      ))}
    </ul>
  );
}

async function fetchSaved(): Promise<SavedPlace[] | "signed_out" | null> {
  const res = await fetch("/api/saved-places");
  if (res.status === 401) return "signed_out";
  return res.ok ? ((await res.json()) as SavedPlace[]) : null;
}
