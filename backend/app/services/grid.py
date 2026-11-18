"""A fixed lon/lat grid used as the unit of caching for Overpass fetches.

Any bounding box maps onto a set of cells. A cell is fetched from Overpass
once (for all categories) and then served from PostGIS until it goes stale,
so panning around an area costs at most a handful of upstream queries.

Cells are CELL_DEGREES on a side: about 2.2 km north-south everywhere, and
narrower east-west away from the equator (about 1.4 km at 50 degrees N).
"""

import math
from dataclasses import dataclass

CELL_DEGREES = 0.02

# Largest area one request may cover (6 x 6 cells, roughly 13 x 9 km in
# mid-latitudes). Beyond that the map should ask the user to zoom in.
MAX_CELLS = 36


@dataclass(frozen=True)
class BBox:
    """West/south/east/north in degrees (WGS 84)."""

    west: float
    south: float
    east: float
    north: float

    @classmethod
    def parse(cls, value: str) -> "BBox":
        """Parse "west,south,east,north" (the order MapLibre's getBounds uses).

        Raises ValueError with a readable message on bad input.
        """
        parts = value.split(",")
        if len(parts) != 4:
            raise ValueError("bbox must be west,south,east,north")
        try:
            west, south, east, north = (float(p) for p in parts)
        except ValueError:
            raise ValueError("bbox values must be numbers") from None
        if not all(math.isfinite(v) for v in (west, south, east, north)):
            raise ValueError("bbox values must be finite")
        if not (-180 <= west < east <= 180):
            raise ValueError("bbox longitudes must satisfy -180 <= west < east <= 180")
        if not (-90 <= south < north <= 90):
            raise ValueError("bbox latitudes must satisfy -90 <= south < north <= 90")
        return cls(west, south, east, north)


Cell = tuple[int, int]  # (ix, iy)


def _index(degrees: float) -> int:
    return math.floor(degrees / CELL_DEGREES)


def cells_for(bbox: BBox) -> set[Cell]:
    """All cells the bbox touches."""
    # The east/north edges are exclusive, so a bbox ending exactly on a cell
    # boundary doesn't pull in the next row/column of cells.
    x0, x1 = _index(bbox.west), math.ceil(bbox.east / CELL_DEGREES) - 1
    y0, y1 = _index(bbox.south), math.ceil(bbox.north / CELL_DEGREES) - 1
    return {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}


def cell_count(bbox: BBox) -> int:
    x0, x1 = _index(bbox.west), math.ceil(bbox.east / CELL_DEGREES) - 1
    y0, y1 = _index(bbox.south), math.ceil(bbox.north / CELL_DEGREES) - 1
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def covering_bbox(cells: set[Cell]) -> BBox:
    """The smallest bbox containing all the given cells (one Overpass query)."""
    xs = [x for x, _ in cells]
    ys = [y for _, y in cells]
    return BBox(
        west=min(xs) * CELL_DEGREES,
        south=min(ys) * CELL_DEGREES,
        east=(max(xs) + 1) * CELL_DEGREES,
        north=(max(ys) + 1) * CELL_DEGREES,
    )
