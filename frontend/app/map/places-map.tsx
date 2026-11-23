// The interactive map: MapLibre GL with OpenFreeMap vector tiles, place
// markers loaded from /api/places as the map moves, category filters, place
// search and a details panel. The side panel's tabs also hold the chat and
// the trip planner, whose places and itineraries are drawn on this map.
//
// A Client Component because it is all browser interactivity. MapLibre itself
// needs `window` and WebGL, so it is imported inside useEffect (only ever in
// the browser) rather than at the top of the file.
"use client";

import "maplibre-gl/dist/maplibre-gl.css";

import type { ExpressionSpecification, GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  CATEGORIES,
  type CategoryId,
  categoryLabel,
  categorySingular,
  type GeocodeHit,
  type GeocodeOut,
  MAP_STYLE_URL,
  MIN_PLACES_ZOOM,
  type PlaceCollection,
  type PlaceDetail,
  type PlaceFeature,
} from "@/lib/map";
import type { TripPlan } from "@/lib/agent";
import { type ChatPlace, distanceM, type MapView } from "@/lib/chat";

import { ChatPanel } from "./chat-panel";
import { PlannerPanel } from "./planner-panel";
import { SavedPanel } from "./saved-panel";

type Tab = "explore" | "ask" | "plan" | "saved";
const TABS: [Tab, string][] = [
  ["explore", "Explore"],
  ["ask", "Ask"],
  ["plan", "Plan"],
  ["saved", "Saved"],
];

// Somewhere with data to show on first load; the theme will decide later.
const START = { center: [-3.19, 55.95] as [number, number], zoom: 14 };
// Wait this long after the map stops moving before asking for places.
const DEBOUNCE_MS = 300;
// Search results for big areas (a city) zoom to here rather than to their
// whole bounding box, so places are visible straight away.
const SEARCH_ZOOM = 14;

type Status =
  | { kind: "loading" }
  | { kind: "zoom_in" }
  | { kind: "ok"; collection: PlaceCollection }
  | { kind: "error"; message: string };

const EMPTY = { type: "FeatureCollection" as const, features: [] };
const ALL_CATEGORIES: CategoryId[] = CATEGORIES.map((c) => c.id);

// A MapLibre style expression: colour each point by its category property,
// grey for anything unknown. MapLibre's type requires at least one
// label/colour pair to be written out, hence `first` before the rest.
const [first, ...rest] = CATEGORIES;
const CATEGORY_COLOR: ExpressionSpecification = [
  "match",
  ["get", "category"],
  first.id,
  first.color,
  ...rest.flatMap((c) => [c.id, c.color]),
  "#525252",
];

export function PlacesMap() {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const readyRef = useRef(false); // style loaded and our layers added
  const categoriesRef = useRef<CategoryId[]>(ALL_CATEGORIES);
  const abortRef = useRef<AbortController | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [categories, setCategories] = useState<CategoryId[]>(ALL_CATEGORIES);
  const [places, setPlaces] = useState<PlaceFeature[]>([]);
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [selected, setSelected] = useState<PlaceDetail | null>(null);
  const [tab, setTab] = useState<Tab>("explore");
  // Places the assistant found, shown as their own, larger markers.
  const [chatPlaces, setChatPlaces] = useState<ChatPlace[]>([]);
  // The trip planner's itinerary: numbered markers joined by a line. Also
  // kept in a ref, in case it arrives before the map has finished loading.
  const [tripPlan, setTripPlan] = useState<TripPlan | null>(null);
  const tripPlanRef = useRef<TripPlan | null>(null);

  // What the chat is told the user is looking at, read as each message is sent.
  const getMapView = useCallback((): MapView | null => {
    const map = mapRef.current;
    if (!map) return null;
    const centre = map.getCenter();
    const corner = map.getBounds().getNorthEast();
    return {
      lat: centre.lat,
      lon: centre.lng,
      radius_m: Math.max(1, Math.round(distanceM(centre.lat, centre.lng, corner.lat, corner.lng))),
      selected_place_id: selected?.id ?? null,
    };
  }, [selected]);

  // These callbacks read state through refs, so they never go stale and can
  // be registered once as MapLibre event listeners.
  const loadPlaces = useCallback(async () => {
    const map = mapRef.current;
    if (!map) return;
    abortRef.current?.abort(); // a newer view replaces any request in flight

    if (map.getZoom() < MIN_PLACES_ZOOM || categoriesRef.current.length === 0) {
      setPlaces([]);
      setStatus(
        categoriesRef.current.length === 0
          ? { kind: "ok", collection: emptyCollection() }
          : { kind: "zoom_in" },
      );
      return;
    }

    const controller = new AbortController();
    abortRef.current = controller;
    const b = map.getBounds();
    const params = new URLSearchParams({
      bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]
        .map((n) => n.toFixed(5))
        .join(","),
    });
    for (const category of categoriesRef.current) params.append("category", category);

    setStatus({ kind: "loading" });
    try {
      const res = await fetch(`/api/places?${params}`, { signal: controller.signal });
      if (res.status === 422) {
        setPlaces([]);
        setStatus({ kind: "zoom_in" });
        return;
      }
      if (res.status === 429) {
        setStatus({
          kind: "error",
          message: "Too many requests. Pause for a moment, then move the map.",
        });
        return;
      }
      if (!res.ok) throw new Error(`places: ${res.status}`);
      const collection = (await res.json()) as PlaceCollection;
      setPlaces(collection.features);
      setStatus({ kind: "ok", collection });
    } catch (error) {
      if (controller.signal.aborted) return;
      console.error(error);
      setStatus({ kind: "error", message: "Couldn't load places. Move the map to try again." });
    }
  }, []);

  const scheduleLoad = useCallback(
    (delay: number) => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => void loadPlaces(), delay);
    },
    [loadPlaces],
  );

  const selectPlace = useCallback(async (id: string) => {
    try {
      const res = await fetch(`/api/places/${encodeURIComponent(id)}`);
      if (!res.ok) throw new Error(`place: ${res.status}`);
      setSelected((await res.json()) as PlaceDetail);
    } catch (error) {
      console.error(error);
    }
  }, []);

  // Create the map once.
  useEffect(() => {
    let cancelled = false;

    void (async () => {
      const maplibregl = await import("maplibre-gl");
      if (cancelled || !containerRef.current) return;
      // Our copy of MapLibre's worker (see scripts/copy-maplibre-worker.mjs).
      maplibregl.setWorkerUrl("/vendor/maplibre-gl-worker.mjs");

      const map = new maplibregl.Map({
        container: containerRef.current,
        style: MAP_STYLE_URL, // includes the OpenFreeMap/OSM attribution
        center: START.center,
        zoom: START.zoom,
      });
      mapRef.current = map;
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

      map.on("load", () => {
        map.addSource("places", { type: "geojson", data: EMPTY });
        map.addLayer({
          id: "places",
          type: "circle",
          source: "places",
          paint: {
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 13, 4, 17, 8],
            "circle-color": CATEGORY_COLOR,
            "circle-stroke-width": 1.5,
            "circle-stroke-color": "#ffffff",
          },
        });
        map.addSource("chat-places", { type: "geojson", data: EMPTY });
        map.addLayer({
          id: "chat-places",
          type: "circle",
          source: "chat-places",
          paint: {
            "circle-radius": 8,
            "circle-color": CATEGORY_COLOR,
            "circle-stroke-width": 3,
            "circle-stroke-color": "#171717",
          },
        });
        map.addLayer({
          id: "places-selected",
          type: "circle",
          source: "places",
          filter: ["==", ["get", "id"], ""],
          paint: {
            "circle-radius": 11,
            "circle-color": "rgba(0,0,0,0)",
            "circle-stroke-width": 3,
            "circle-stroke-color": "#171717",
          },
        });
        // The itinerary, above everything else.
        map.addSource("trip", { type: "geojson", data: EMPTY });
        map.addLayer({
          id: "trip-line",
          type: "line",
          source: "trip",
          filter: ["==", ["geometry-type"], "LineString"],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#171717", "line-width": 3, "line-dasharray": [1, 2] },
        });
        map.addLayer({
          id: "trip-stops",
          type: "circle",
          source: "trip",
          filter: ["==", ["geometry-type"], "Point"],
          paint: {
            "circle-radius": 11,
            "circle-color": "#171717",
            "circle-stroke-width": 2,
            "circle-stroke-color": "#ffffff",
          },
        });
        map.addLayer({
          id: "trip-labels",
          type: "symbol",
          source: "trip",
          filter: ["==", ["geometry-type"], "Point"],
          layout: {
            "text-field": ["get", "label"],
            // A font the tile style provides (OpenFreeMap serves Noto Sans).
            "text-font": ["Noto Sans Bold"],
            "text-size": 12,
            "text-allow-overlap": true,
            "text-ignore-placement": true,
          },
          paint: { "text-color": "#ffffff" },
        });
        map.on("click", "places", (event) => {
          const id = event.features?.[0]?.properties?.id;
          if (typeof id === "string") void selectPlace(id);
        });
        map.on("click", "chat-places", (event) => {
          const id = event.features?.[0]?.properties?.place_id;
          if (typeof id === "string") void selectPlace(id);
        });
        map.on("click", "trip-stops", (event) => {
          const id = event.features?.[0]?.properties?.place_id;
          if (typeof id === "string" && id) void selectPlace(id);
        });
        for (const layer of ["places", "chat-places", "trip-stops"]) {
          map.on("mouseenter", layer, () => (map.getCanvas().style.cursor = "pointer"));
          map.on("mouseleave", layer, () => (map.getCanvas().style.cursor = ""));
        }
        readyRef.current = true;
        if (tripPlanRef.current) drawTrip(map, tripPlanRef.current);
        scheduleLoad(0);
      });
      map.on("moveend", () => scheduleLoad(DEBOUNCE_MS));
    })();

    return () => {
      cancelled = true;
      abortRef.current?.abort();
      if (debounceRef.current) clearTimeout(debounceRef.current);
      readyRef.current = false;
      mapRef.current?.remove();
      mapRef.current = null;
    };
  }, [scheduleLoad, selectPlace]);

  // Push places and the selection into the map's data source.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    (map.getSource("places") as GeoJSONSource | undefined)?.setData({
      type: "FeatureCollection",
      // MapLibre filters read properties, so copy the id into them.
      features: places.map((f) => ({ ...f, properties: { ...f.properties, id: f.id } })),
    });
    map.setFilter("places-selected", ["==", ["get", "id"], selected?.id ?? ""]);
  }, [places, selected]);

  // Show the assistant's places and bring them into view.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !readyRef.current) return;
    (map.getSource("chat-places") as GeoJSONSource | undefined)?.setData({
      type: "FeatureCollection",
      features: chatPlaces.map((p) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [p.lon, p.lat] },
        properties: { place_id: p.place_id, name: p.name, category: p.category },
      })),
    });
    if (chatPlaces.length === 1) {
      map.flyTo({
        center: [chatPlaces[0].lon, chatPlaces[0].lat],
        zoom: Math.max(map.getZoom(), 16),
      });
    } else if (chatPlaces.length > 1) {
      const lons = chatPlaces.map((p) => p.lon);
      const lats = chatPlaces.map((p) => p.lat);
      map.fitBounds([Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)], {
        padding: 60,
        maxZoom: 16,
      });
    }
  }, [chatPlaces]);

  // Draw the itinerary (or clear it) and bring it into view.
  useEffect(() => {
    tripPlanRef.current = tripPlan;
    const map = mapRef.current;
    if (map && readyRef.current) drawTrip(map, tripPlan);
  }, [tripPlan]);

  function toggleCategory(id: CategoryId) {
    const next = categories.includes(id) ? categories.filter((c) => c !== id) : [...categories, id];
    categoriesRef.current = next;
    setCategories(next);
    scheduleLoad(0);
  }

  function goTo(hit: GeocodeHit) {
    const map = mapRef.current;
    if (!map) return;
    setSelected(null); // the selected place is probably no longer in view
    const [west, south, east, north] = hit.bbox;
    const camera = map.cameraForBounds([west, south, east, north], { padding: 40, maxZoom: 16 });
    if (camera?.zoom !== undefined && camera.zoom >= SEARCH_ZOOM) {
      map.fitBounds([west, south, east, north], { padding: 40, maxZoom: 16 });
    } else {
      map.flyTo({ center: [hit.lon, hit.lat], zoom: SEARCH_ZOOM });
    }
  }

  function showPlace(feature: PlaceFeature) {
    mapRef.current?.flyTo({
      center: feature.geometry.coordinates,
      zoom: Math.max(mapRef.current.getZoom(), 16),
    });
    void selectPlace(feature.id);
  }

  return (
    <div className="flex flex-1 flex-col md:min-h-0 md:flex-row">
      <aside className="flex flex-col gap-4 border-b border-neutral-200 p-4 md:w-96 md:shrink-0 md:overflow-y-auto md:border-r md:border-b-0 dark:border-neutral-800">
        <div role="tablist" aria-label="Panel" className="flex gap-1 text-sm">
          {TABS.map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`tab-${id}`}
              aria-selected={tab === id}
              aria-controls={`panel-${id}`}
              onClick={() => setTab(id)}
              className={`rounded-md px-3 py-1 ${
                tab === id
                  ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900"
                  : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        {selected && <PlaceCard place={selected} onClose={() => setSelected(null)} />}

        {/* Panels stay mounted when hidden, so a conversation survives
            switching tabs. */}
        <div
          role="tabpanel"
          id="panel-ask"
          aria-labelledby="tab-ask"
          hidden={tab !== "ask"}
          className="flex flex-col gap-4"
        >
          <ChatPanel
            onPlaces={setChatPlaces}
            onShowPlace={(place) => {
              mapRef.current?.flyTo({ center: [place.lon, place.lat], zoom: 16 });
              void selectPlace(place.place_id);
            }}
            getMapView={getMapView}
          />
        </div>
        <div
          role="tabpanel"
          id="panel-plan"
          aria-labelledby="tab-plan"
          hidden={tab !== "plan"}
          className="flex flex-col gap-4"
        >
          <PlannerPanel
            onPlan={setTripPlan}
            onShow={(stop) => {
              mapRef.current?.flyTo({ center: [stop.lon, stop.lat], zoom: 16 });
              if (stop.place_id) void selectPlace(stop.place_id);
            }}
          />
        </div>
        <div role="tabpanel" id="panel-saved" aria-labelledby="tab-saved" hidden={tab !== "saved"}>
          {tab === "saved" && (
            <SavedPanel
              onShow={(place) => {
                mapRef.current?.flyTo({ center: [place.lon, place.lat], zoom: 16 });
                void selectPlace(place.place_id);
              }}
            />
          )}
        </div>
        <div
          role="tabpanel"
          id="panel-explore"
          aria-labelledby="tab-explore"
          hidden={tab !== "explore"}
          className="flex flex-col gap-4"
        >
          <PlaceSearch onPick={goTo} />
          <CategoryFilter selected={categories} onToggle={toggleCategory} />
          <StatusLine status={status} />
          {places.length > 0 && (
            <ul className="flex flex-col divide-y divide-neutral-200 text-sm dark:divide-neutral-800">
              {places.map((feature) => (
                <li key={feature.id}>
                  <button
                    type="button"
                    onClick={() => showPlace(feature)}
                    className="flex w-full items-center gap-2 py-1.5 text-left hover:underline"
                  >
                    <CategoryDot category={feature.properties.category} />
                    <span className="truncate">
                      {feature.properties.name ?? (
                        <span className="text-neutral-500">
                          Unnamed {categorySingular(feature.properties.category)}
                        </span>
                      )}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
      {/* Sized with flexbox, not `absolute inset-0`: MapLibre adds its own
          `position: relative` to the container, which would cancel that out
          and collapse the map to zero height. */}
      <div className="flex min-h-[60vh] flex-1 flex-col">
        <div ref={containerRef} className="flex-1" aria-label="Map" role="region" />
      </div>
    </div>
  );
}

/**
 * Put a trip plan on the map: "S" at the start, a numbered marker per stop,
 * and a dashed line through them in order (back to the start if the plan
 * returns there). A null plan clears it.
 */
function drawTrip(map: MapLibreMap, plan: TripPlan | null) {
  const source = map.getSource("trip") as GeoJSONSource | undefined;
  if (!source) return;
  if (!plan) {
    source.setData(EMPTY);
    return;
  }
  const start: [number, number] = [plan.start.lon, plan.start.lat];
  const path = [start, ...plan.stops.map((s): [number, number] => [s.lon, s.lat])];
  if (plan.return) path.push(start);
  source.setData({
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        geometry: { type: "LineString", coordinates: path },
        properties: {},
      },
      {
        type: "Feature",
        geometry: { type: "Point", coordinates: start },
        properties: { label: "S", place_id: "" },
      },
      ...plan.stops.map((stop, i) => ({
        type: "Feature" as const,
        geometry: { type: "Point" as const, coordinates: [stop.lon, stop.lat] },
        properties: { label: String(i + 1), place_id: stop.place_id },
      })),
    ],
  });
  const lons = path.map(([lon]) => lon);
  const lats = path.map(([, lat]) => lat);
  map.fitBounds([Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)], {
    padding: 60,
    maxZoom: 16,
  });
}

function emptyCollection(): PlaceCollection {
  return {
    type: "FeatureCollection",
    features: [],
    complete: true,
    incomplete_reason: null,
    truncated: false,
  };
}

function PlaceSearch({ onPick }: { onPick: (hit: GeocodeHit) => void }) {
  const [results, setResults] = useState<GeocodeHit[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  // Runs on submit only: Nominatim's usage policy forbids search-as-you-type.
  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const q = String(new FormData(event.currentTarget).get("q") ?? "").trim();
    if (q.length < 2) return;
    setPending(true);
    setMessage(null);
    try {
      const res = await fetch(`/api/geocode?${new URLSearchParams({ q })}`);
      if (res.status === 429) throw new Error("Too many searches. Try again in a minute.");
      if (!res.ok) throw new Error("Search is unavailable right now. Try again shortly.");
      const body = (await res.json()) as GeocodeOut;
      setResults(body.results);
      if (body.results.length === 0) setMessage("No matches found.");
      else onPick(body.results[0]);
    } catch (error) {
      setResults(null);
      setMessage(error instanceof Error ? error.message : "Search failed.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <form onSubmit={onSubmit} role="search" className="flex gap-2">
        <label htmlFor="map-search" className="sr-only">
          Search for a place
        </label>
        <input
          id="map-search"
          name="q"
          type="search"
          minLength={2}
          maxLength={200}
          required
          placeholder="Search a town or address"
          className="min-w-0 flex-1 rounded-md border border-neutral-300 bg-transparent px-3 py-1.5 text-sm dark:border-neutral-700"
        />
        <button
          type="submit"
          disabled={pending}
          className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm text-white disabled:opacity-60 dark:bg-white dark:text-neutral-900"
        >
          {pending ? "…" : "Search"}
        </button>
      </form>
      {message && <p className="text-sm text-neutral-500">{message}</p>}
      {results && results.length > 1 && (
        <ul className="flex flex-col text-sm">
          {results.map((hit) => (
            <li key={`${hit.lon},${hit.lat}`}>
              <button
                type="button"
                onClick={() => onPick(hit)}
                title={hit.display_name}
                className="w-full truncate py-0.5 text-left text-neutral-600 hover:underline dark:text-neutral-400"
              >
                {hit.display_name}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CategoryFilter({
  selected,
  onToggle,
}: {
  selected: CategoryId[];
  onToggle: (id: CategoryId) => void;
}) {
  return (
    <fieldset className="flex flex-wrap gap-1.5">
      <legend className="sr-only">Show categories</legend>
      {CATEGORIES.map((category) => {
        const on = selected.includes(category.id);
        return (
          <button
            key={category.id}
            type="button"
            aria-pressed={on}
            onClick={() => onToggle(category.id)}
            className={`flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs ${
              on
                ? "border-neutral-400 dark:border-neutral-500"
                : "border-neutral-200 text-neutral-400 dark:border-neutral-800"
            }`}
          >
            <span
              className="inline-block size-2 rounded-full"
              style={{
                backgroundColor: on ? category.color : "transparent",
                border: `1px solid ${category.color}`,
              }}
            />
            {category.label}
          </button>
        );
      })}
    </fieldset>
  );
}

const INCOMPLETE_MESSAGES = {
  budget:
    "Today's limit for fetching new map data has been reached, so some areas may be empty. Places already loaded are still shown.",
  cooldown: "The place data service is busy, so some areas may be empty. Try again in a minute.",
  upstream_error:
    "The place data service didn't respond, so some areas may be empty. Try again in a minute.",
  busy: "Still loading data for this area. Move the map again in a moment.",
} as const;

function StatusLine({ status }: { status: Status }) {
  let text: string;
  let note: string | null = null;
  switch (status.kind) {
    case "loading":
      text = "Loading places…";
      break;
    case "zoom_in":
      text = "Zoom in to see places.";
      break;
    case "error":
      text = status.message;
      break;
    case "ok": {
      const { features, truncated, incomplete_reason } = status.collection;
      text = `${features.length} place${features.length === 1 ? "" : "s"} in view${truncated ? " (zoom in to see all)" : ""}.`;
      note = incomplete_reason ? INCOMPLETE_MESSAGES[incomplete_reason] : null;
    }
  }
  return (
    <div role="status" className="flex flex-col gap-1 text-sm">
      <p className="text-neutral-600 dark:text-neutral-400">{text}</p>
      {note && <p className="text-amber-700 dark:text-amber-400">{note}</p>}
    </div>
  );
}

// Tags worth showing, in display order, with labels.
const DETAIL_TAGS: [string, string][] = [
  ["opening_hours", "Hours"],
  ["cuisine", "Cuisine"],
  ["wheelchair", "Wheelchair access"],
  ["phone", "Phone"],
];

function PlaceCard({ place, onClose }: { place: PlaceDetail; onClose: () => void }) {
  const { tags } = place.properties;
  const address = [tags["addr:housenumber"], tags["addr:street"]].filter(Boolean).join(" ");
  const website = safeHttpUrl(tags.website ?? tags["contact:website"]);

  return (
    <section className="flex flex-col gap-2 rounded-lg border border-neutral-200 p-3 text-sm dark:border-neutral-800">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="font-medium">{place.properties.name ?? "Unnamed place"}</h2>
          <p className="flex items-center gap-1.5 text-neutral-500">
            <CategoryDot category={place.properties.category} />
            {categoryLabel(place.properties.category)}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close details"
          className="text-neutral-500"
        >
          ✕
        </button>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        {address && (
          <>
            <dt className="text-neutral-500">Address</dt>
            <dd>{address}</dd>
          </>
        )}
        {DETAIL_TAGS.filter(([key]) => tags[key]).map(([key, label]) => (
          <div key={key} className="contents">
            <dt className="text-neutral-500">{label}</dt>
            <dd className="break-words">{tags[key].replaceAll(";", ", ")}</dd>
          </div>
        ))}
        {website && (
          <>
            <dt className="text-neutral-500">Website</dt>
            <dd className="truncate">
              <a
                href={website}
                target="_blank"
                rel="noopener noreferrer nofollow"
                className="underline"
              >
                {new URL(website).host}
              </a>
            </dd>
          </>
        )}
      </dl>
      <a
        href={place.properties.osm_url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-xs text-neutral-500 underline"
      >
        View on OpenStreetMap
      </a>
    </section>
  );
}

/**
 * The URL if it is http(s), else null.
 *
 * OSM tags are written by anyone, so a "website" could be `javascript:...`.
 * Only real web links become clickable.
 */
function safeHttpUrl(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value.includes("://") ? value : `https://${value}`);
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function CategoryDot({ category }: { category: string }) {
  const color = CATEGORIES.find((c) => c.id === category)?.color ?? "#525252";
  return (
    <span
      className="inline-block size-2 shrink-0 rounded-full"
      style={{ backgroundColor: color }}
    />
  );
}
