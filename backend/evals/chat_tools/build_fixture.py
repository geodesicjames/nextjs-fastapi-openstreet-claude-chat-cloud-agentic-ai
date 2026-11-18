"""Snapshot the map data the chat tool-use eval runs against.

Reads the places, grid cells and geocoding answers already cached in a
database (normally the dev DB) and writes them to fixture.json.gz. Every
eval run loads this file into a fresh, throwaway database, so the tools see
exactly the same data each time and no OpenStreetMap service is called.

    cd backend && uv run python -m evals.chat_tools.build_fixture

Re-run it only when you deliberately want new data; changing the fixture
changes the eval, so scores from before and after aren't comparable.

Place data is from OpenStreetMap (ODbL): © OpenStreetMap contributors.
"""

import asyncio
import gzip
import json
from pathlib import Path

from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import select

from app.db import SessionLocal, engine
from app.models import GeocodeResult, Place, PlaceCell
from app.services.geocoding import normalize_query

HERE = Path(__file__).parent
FIXTURE = HERE / "fixture.json.gz"

# The places the eval's questions mention. The gazetteer stub matches a
# query that contains any alias (lower-case) and answers with that place's
# real geocoding result, taken from the cache under `cached_query`.
GAZETTEER = [
    {"cached_query": "leith walk, edinburgh", "aliases": ["leith walk"]},
    {"cached_query": "the meadows, edinburgh", "aliases": ["meadows"]},
    {"cached_query": "bruntsfield, edinburgh", "aliases": ["bruntsfield"]},
    {"cached_query": "stockbridge, edinburgh", "aliases": ["stockbridge"]},
    {"cached_query": "calton hill, edinburgh", "aliases": ["calton hill"]},
    {"cached_query": "princes street, edinburgh", "aliases": ["princes street", "princes st"]},
    {"cached_query": "royal mile, edinburgh", "aliases": ["royal mile"]},
    {"cached_query": "haymarket, edinburgh", "aliases": ["haymarket"]},
    {"cached_query": "arthur's seat, edinburgh", "aliases": ["arthur's seat", "arthurs seat"]},
    {"cached_query": "grassmarket, edinburgh", "aliases": ["grassmarket"]},
    {"cached_query": "dean village, edinburgh", "aliases": ["dean village"]},
    {"cached_query": "marchmont, edinburgh", "aliases": ["marchmont"]},
    {"cached_query": "glasgow city centre", "aliases": ["glasgow"]},
]


async def main() -> None:
    async with SessionLocal() as db:
        cells = [(c.ix, c.iy) for c in await db.scalars(select(PlaceCell))]
        rows = await db.execute(
            select(Place, ST_X(Place.geom), ST_Y(Place.geom)).order_by(Place.osm_type, Place.osm_id)
        )
        places = [
            {
                "osm_type": p.osm_type,
                "osm_id": p.osm_id,
                "name": p.name,
                "category": p.category,
                "tags": p.tags,
                "lon": float(lon),
                "lat": float(lat),
            }
            for p, lon, lat in rows
        ]
        gazetteer = []
        for entry in GAZETTEER:
            cached = await db.get(GeocodeResult, normalize_query(entry["cached_query"]))
            if cached is None or not cached.results:
                raise SystemExit(
                    f"No cached geocoding answer for {entry['cached_query']!r}; geocode it first."
                )
            gazetteer.append({"aliases": entry["aliases"], "results": cached.results})
    await engine.dispose()

    data = {
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "cells": sorted(cells),
        "places": places,
        "gazetteer": gazetteer,
    }
    FIXTURE.write_bytes(
        gzip.compress(json.dumps(data, ensure_ascii=False, sort_keys=True).encode(), mtime=0)
    )
    print(
        f"Wrote {FIXTURE.name}: {len(places)} places, {len(cells)} cells, "
        f"{len(gazetteer)} gazetteer entries, {FIXTURE.stat().st_size / 1024:.0f} KB"
    )


if __name__ == "__main__":
    asyncio.run(main())
