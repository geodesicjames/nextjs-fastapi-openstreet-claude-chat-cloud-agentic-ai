"""Place categories shown on the map, and the OSM tags that define them.

Kept as data so the list can follow the project's theme once it is chosen.
Each category maps to one OSM key=value pair; see
https://wiki.openstreetmap.org/wiki/Map_features for what's available.
"""

from typing import Any

CATEGORIES: dict[str, tuple[str, str]] = {
    "cafe": ("amenity", "cafe"),
    "restaurant": ("amenity", "restaurant"),
    "park": ("leisure", "park"),
    "drinking_water": ("amenity", "drinking_water"),
    "toilets": ("amenity", "toilets"),
    "viewpoint": ("tourism", "viewpoint"),
}


def category_of(tags: dict[str, Any]) -> str | None:
    """The first category whose tag matches, or None."""
    for category, (key, value) in CATEGORIES.items():
        if tags.get(key) == value:
            return category
    return None
