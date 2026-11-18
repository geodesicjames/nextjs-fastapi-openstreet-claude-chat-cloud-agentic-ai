// /map: a Server Component shell around the interactive map. The page
// itself is static (prerendered); everything that moves is in <PlacesMap>,
// which runs in the browser.
import type { Metadata } from "next";

import { PlacesMap } from "./places-map";

export const metadata: Metadata = { title: "Map · Places" };

export default function MapPage() {
  return (
    // On wide screens the map fills the viewport below the site header
    // (49px: py-3 plus one line of text plus the border), and the side
    // panel scrolls on its own. On small screens the page scrolls normally.
    <main className="flex flex-1 flex-col md:h-[calc(100dvh-49px)] md:flex-none">
      <h1 className="sr-only">Map</h1>
      <PlacesMap />
    </main>
  );
}
