// Map types and constants shared by the map components (browser code) and
// mirrored from the backend schemas.

// Mirrors CATEGORIES in backend/app/services/categories.py; keep in sync.
export const CATEGORIES = [
  { id: "cafe", label: "Cafés", singular: "café", color: "#b45309" },
  { id: "restaurant", label: "Restaurants", singular: "restaurant", color: "#dc2626" },
  { id: "park", label: "Parks", singular: "park", color: "#16a34a" },
  {
    id: "drinking_water",
    label: "Drinking water",
    singular: "drinking water tap",
    color: "#0284c7",
  },
  { id: "toilets", label: "Toilets", singular: "toilet", color: "#7c3aed" },
  { id: "viewpoint", label: "Viewpoints", singular: "viewpoint", color: "#db2777" },
] as const;

export type CategoryId = (typeof CATEGORIES)[number]["id"];

export function categoryLabel(id: string): string {
  return CATEGORIES.find((c) => c.id === id)?.label ?? id;
}

/** "restaurant", for phrases like "Unnamed restaurant". */
export function categorySingular(id: string): string {
  return CATEGORIES.find((c) => c.id === id)?.singular ?? id;
}

// Vector tiles: public style URL, no key (OpenFreeMap). Overridable for a
// keyed provider; tile keys are public by design, hence NEXT_PUBLIC_.
export const MAP_STYLE_URL =
  process.env.NEXT_PUBLIC_MAP_STYLE_URL ?? "https://tiles.openfreemap.org/styles/liberty";

// Below this zoom the visible area exceeds the backend's per-request limit.
export const MIN_PLACES_ZOOM = 13;

// Mirrors backend/app/schemas/places.py.
export type IncompleteReason = "budget" | "cooldown" | "upstream_error" | "busy";

export type PlaceFeature = {
  type: "Feature";
  id: string;
  geometry: { type: "Point"; coordinates: [number, number] };
  properties: { name: string | null; category: string; osm_type: string; osm_id: number };
};

export type PlaceCollection = {
  type: "FeatureCollection";
  features: PlaceFeature[];
  complete: boolean;
  incomplete_reason: IncompleteReason | null;
  truncated: boolean;
};

export type PlaceDetail = Omit<PlaceFeature, "properties"> & {
  properties: PlaceFeature["properties"] & { tags: Record<string, string>; osm_url: string };
};

// Mirrors backend/app/schemas/geocode.py.
export type GeocodeHit = {
  name: string;
  display_name: string;
  lon: number;
  lat: number;
  bbox: [number, number, number, number]; // west, south, east, north
  kind: string;
};

export type GeocodeOut = { results: GeocodeHit[]; stale: boolean };
