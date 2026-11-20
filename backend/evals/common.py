"""Shared plumbing for the evals (chat_tools/ and trip_planner/).

Import this module before anything from `app.db`: it points the app at a
throwaway eval database (<your DB name>_eval) by setting DATABASE_URL, and
app.db builds its engine from settings at import time (as in
tests/conftest.py).

What's here:
- the eval database: recreate it, load the frozen map fixture into it;
- stubs for the external services: Overpass (the fixture's cells are fresh;
  anything outside them reports "data may be missing") and Nominatim (a
  gazetteer of known places), so runs are reproducible and cost nothing in
  OSM usage;
- the harness gate: a runner refuses to start if its own files changed
  since you last approved them, so a score can't silently come from a
  different eval;
- small helpers for results files and confidence intervals.
"""

import gzip
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import make_url

BACKEND = Path(__file__).resolve().parents[1]
HILLCLIMB = BACKEND.parent / ".claude" / "hillclimb"
FIXTURE = Path(__file__).resolve().parent / "chat_tools" / "fixture.json.gz"


def _eval_database_url() -> str:
    from app.config import get_settings

    url = make_url(str(get_settings().database_url))
    return url.set(database=f"{url.database}_eval").render_as_string(hide_password=False)


os.environ["DATABASE_URL"] = _eval_database_url()
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from alembic import command  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Place, PlaceCell  # noqa: E402
from app.services.geocoding import NominatimClient  # noqa: E402
from app.services.grid import BBox  # noqa: E402
from app.services.overpass import OverpassClient, OverpassUnavailableError  # noqa: E402

# --- Harness gate ---------------------------------------------------------------------------


class Harness:
    """The files a runner's scores depend on, and where their approval is recorded."""

    def __init__(self, flow_dir: Path, files: list[Path], default_state: dict[str, Any]) -> None:
        self.flow_dir = flow_dir
        self.files = files
        self.default_state = {
            "harness_paths": [str(p.relative_to(BACKEND.parent)) for p in files],
            **default_state,
        }

    def sha(self) -> str:
        digest = hashlib.sha256()
        for path in self.files:
            digest.update(path.name.encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    def _state_path(self) -> Path:
        return self.flow_dir / "_state.json"

    def state(self) -> dict[str, Any]:
        path = self._state_path()
        stored = json.loads(path.read_text()) if path.exists() else {}
        return {**self.default_state, **stored}

    def approve(self) -> str:
        state = self.state()
        state["harness_paths"] = self.default_state["harness_paths"]
        state["harness_sha"] = self.sha()
        self.flow_dir.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(json.dumps(state, indent=2) + "\n")
        return f"Approved harness {state['harness_sha'][:12]} ({', '.join(state['harness_paths'])})"

    def approved(self) -> bool:
        return self.state().get("harness_sha") == self.sha()


# --- Database for the run -------------------------------------------------------------------


def load_fixture_file() -> dict[str, Any]:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


async def recreate_database() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    assert url.database and url.database.endswith("_eval"), url.database
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()


def migrate() -> None:
    """Run outside the event loop: alembic's env.py starts its own."""
    command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")


async def load_fixture(fixture: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        db.add_all(PlaceCell(ix=ix, iy=iy, fetched_at=now) for ix, iy in fixture["cells"])
        db.add_all(
            Place(
                osm_type=p["osm_type"],
                osm_id=p["osm_id"],
                name=p["name"],
                category=p["category"],
                tags=p["tags"],
                geom=f"SRID=4326;POINT({p['lon']} {p['lat']})",
                fetched_at=now,
            )
            for p in fixture["places"]
        )
        await db.commit()


def gazetteer_coords(fixture: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Every gazetteer alias ("leith walk", "arthurs seat"...) -> (lat, lon)."""
    return {
        alias: (e["results"][0]["lat"], e["results"][0]["lon"])
        for e in fixture["gazetteer"]
        for alias in e["aliases"]
    }


# --- Stubs for the external services ---------------------------------------------------------


@dataclass
class FixtureOverpass(OverpassClient):
    """No live Overpass: areas outside the fixture report missing data."""

    async def fetch(self, db: Any, settings: Any, bbox: BBox) -> list[Any]:
        raise OverpassUnavailableError("upstream_error")


@dataclass
class GazetteerNominatim(NominatimClient):
    """No live Nominatim: answers from the fixture's gazetteer."""

    gazetteer: list[dict[str, Any]] = field(default_factory=list)

    async def search(self, db: Any, settings: Any, query: str) -> list[dict[str, Any]]:
        q = query.lower()
        for entry in self.gazetteer:
            if any(alias in q for alias in entry["aliases"]):
                return entry["results"]
        return []


# --- Results ---------------------------------------------------------------------------------


def append_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_rows(path: Path) -> list[dict[str, Any]]:
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line]
        if path.exists()
        else []
    )


def done_keys(results: Path) -> set[tuple[str, int]]:
    return {(r["prompt_id"], r["rep"]) for r in read_rows(results)}


def wilson(passes: float, n: int) -> tuple[float, float]:
    """95% confidence interval for a pass rate (Wilson score interval)."""
    if n == 0:
        return (0.0, 0.0)
    z, p = 1.96, passes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))
