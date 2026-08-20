"""
Fire Tracker backend.

Serves:
- GET /                        -> frontend (static/index.html)
- GET /api/fires                -> live fire detections from NASA FIRMS (VIIRS NRT), as GeoJSON-ish points
- POST /api/wind/batch          -> wind for many locations in a single Open-Meteo request
- POST /api/wind/forecast/batch -> hourly wind + humidity forecast for many locations, for the higher prediction-strength tiers
- POST /api/elevation/batch     -> ground elevation for many locations, used for the terrain/slope prediction tier
- GET /api/geocode               -> place name/address -> lat/lng, for the address risk-check feature

Fire data merges two sources into one in-memory dataset (see _world_fires):
NASA FIRMS (VIIRS satellite, worldwide) and, Canada-only for now, CWFIS's
national ground-confirmed active fire list (see _fetch_canada_fires).

NASA FIRMS requires a free MAP_KEY: https://firms.modaps.eosdis.nasa.gov/api/
Set it as the FIRMS_MAP_KEY environment variable before running.
"""
import asyncio
import dataclasses
import gzip
import json
import math
import os
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()  # picks up FIRMS_MAP_KEY from a local .env file, so it survives server restarts
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

FIRMS_MAP_KEY = os.environ.get("FIRMS_MAP_KEY", "")
# All 3 current VIIRS NRT satellites, not just SNPP - one alone misses a
# large share of active fires (a satellite only passes over a spot a
# couple times a day). All ~375m resolution and directly comparable.
FIRMS_SOURCES = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]
# Day range of 2, not 1 - NASA's NRT feed occasionally has a short
# ingestion gap where the most recent ~24h isn't processed yet.
FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/2"

app = FastAPI(title="Fire Tracker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Compresses responses over 1KB - the raw (non-clustered) fire lists a
# country search returns can run to hundreds of KB of JSON; gzip shrinks
# that substantially for near-zero CPU cost.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# One shared, connection-pooling client for every outbound request this
# server makes (FIRMS, CWFIS, Open-Meteo, Nominatim), instead of opening a
# brand new client - and paying a fresh TCP/TLS handshake - for every single
# request/chunk. Created at startup, closed at shutdown; per-call timeouts
# still override the default via `timeout=` on each request.
_http_client: httpx.AsyncClient | None = None

# A plain dict per fire (what this used to be) has real per-object overhead
# in CPython - at world scale (~600K raw detections across 3 satellites)
# that was measured at ~430MB RSS just for this one in-memory list, uncomfortably
# close to Render's 512MB free-tier ceiling before any request had even come
# in. @dataclass(slots=True) drops that by avoiding each fire's own hash
# table entirely. Kept subscriptable (__getitem__/get) so every existing
# `f["lat"]` / `f.get("ha")` read site elsewhere in this file didn't need to
# change - only the two construction sites below do.
@dataclasses.dataclass(slots=True)
class Fire:
    lat: float
    lng: float
    confidence: str
    frp: float
    acq_date: str
    acq_time: str
    ha: float | None = None  # hectares burned - ground-sourced fires only

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)


async def _fetch_firms_fires(area: str, source: str) -> list[dict]:
    url = FIRMS_URL.format(key=FIRMS_MAP_KEY, source=source, area=area)
    resp = await _http_client.get(url, timeout=30)
    resp.raise_for_status()

    lines = resp.text.strip().splitlines()
    if not lines:
        return []

    header = lines[0].split(",")
    idx = {name: i for i, name in enumerate(header)}

    # brightness (bright_ti4) isn't kept - nothing on the frontend uses it.
    fires = []
    for line in lines[1:]:
        cols = line.split(",")
        try:
            fires.append(
                Fire(
                    lat=float(cols[idx["latitude"]]),
                    lng=float(cols[idx["longitude"]]),
                    confidence=cols[idx["confidence"]],
                    frp=float(cols[idx["frp"]]),  # fire radiative power (MW), proxy for intensity
                    acq_date=cols[idx["acq_date"]],
                    acq_time=cols[idx["acq_time"]],
                )
            )
        except (ValueError, IndexError, KeyError):
            continue
    return fires


CANADA_FIRES_URL = "https://cwfis.cfs.nrcan.gc.ca/downloads/reportedfires/activefires.csv"
# Stages that mean "currently burning" - EX (extinguished) is the only one
# that isn't. BM ("being monitored") is included: many agencies (e.g.
# Ontario) deliberately don't suppress remote fires that aren't threatening
# anything - still a genuine active fire, just a different response posture.
CANADA_ACTIVE_STAGES = {"OC", "UC", "BH", "BM"}


async def _fetch_canada_fires() -> list[dict]:
    """All of Canada's ground-confirmed active fires in one request - CWFIS
    (Natural Resources Canada)'s national aggregation of every province/
    territory's agency-reported data. No key required.

    Closes a real gap in satellite-only detection: a large, well-established
    fire complex can generate enough smoke to obscure VIIRS/MODIS thermal
    detection over its own hottest core, but ground/dispatch reporting still
    knows it's there. Canada-only for now, not deduplicated against
    satellite hotspots for the same fire beyond the radius-based check
    below - both are real signal."""
    resp = await _http_client.get(CANADA_FIRES_URL, timeout=20)
    resp.raise_for_status()

    lines = resp.text.strip().splitlines()
    if not lines:
        return []
    header = lines[0].split(",")
    idx = {name: i for i, name in enumerate(header)}

    fires = []
    for line in lines[1:]:
        cols = line.split(",")
        try:
            if cols[idx["stage_of_control_status"]] not in CANADA_ACTIVE_STAGES:
                continue
            lat = float(cols[idx["latitude"]])
            lng = float(cols[idx["longitude"]])
            # CWFIS uses -1 as a "size not yet known" sentinel, not a real
            # negative area - clamped to 0 since math.log1p below requires > -1.
            hectares = max(0.0, float(cols[idx["fire_size"]] or 0))
            status_str = cols[idx["status_date"]] or cols[idx["situation_report_date"]]
            dt = datetime.fromisoformat(status_str) if status_str else datetime.now(timezone.utc)
            fires.append(
                Fire(
                    lat=lat,
                    lng=lng,
                    # Ground-confirmed by agency dispatch - the highest
                    # confidence tier this app has.
                    confidence="h",
                    # No FRP for ground-confirmed fires - hectares burned is
                    # the closest severity proxy, log-scaled and floored
                    # above LARGE_FIRE_MIN_FRP so it always renders as its
                    # own marker (a real, verified incident).
                    frp=max(30.0, math.log1p(hectares) * 15),
                    acq_date=dt.strftime("%Y-%m-%d"),
                    acq_time=dt.strftime("%H%M"),
                    # Real hectares, kept alongside the derived FRP proxy so
                    # ground fires can be filtered by actual size (min_hectares
                    # below). Satellite detections leave this None.
                    ha=hectares,
                )
            )
        except (TypeError, ValueError, IndexError, KeyError):
            # Skip just this row - a bad one shouldn't drop the whole refresh.
            continue
    return fires


WORLD_AREA = "-180,-90,180,90"
WORLD_REFRESH_INTERVAL_SECONDS = 10 * 60  # matches FIRMS' own NRT refresh cadence

# The entire world's fire data, held in memory and refreshed on a timer, not
# fetched per-request - a per-area lazy cache only speeds up repeat visits,
# since panning anywhere new pays NASA's ~5-10s whole-world CSV generation
# time live. The whole world across 3 satellites is small enough (a few
# hundred thousand rows) to hold in memory and filter/cluster in plain
# Python, so every /api/fires request is a local list comprehension.
_world_fires: list[dict] = []

# The world (bbox=None, grid set) response - what every visitor's first
# page load requests - takes real CPU: clustering, JSON-encoding, json.dumps,
# and gzip compression, measured at ~2.6s combined for the full dataset.
# Many people loading the map at once used to each redo all of that from
# scratch; this caches BOTH the plain and pre-gzipped JSON bytes, so a
# cache hit skips straight to sending a response - no re-clustering,
# re-serializing, or re-compressing. (Storing the compressed variant too,
# rather than relying on GZipMiddleware to compress the cached bytes fresh
# each time, is what actually removes that per-request cost - middleware
# has no way to know two different requests want the identical output.)
# Cleared whenever _world_fires refreshes (see _refresh_world_fires), so
# it's never more than one refresh cycle (10 min) stale - the same
# staleness the raw data already has.
_world_view_cache: dict[tuple, tuple[bytes, bytes]] = {}  # key -> (plain_json, gzipped_json)


# ~0.15deg (~15-17km) - a VIIRS pixel this close to a ground-reported fire
# is treated as a detection of that same fire, not a separate one. Without
# this, a large fire renders as one ground marker PLUS a dense cluster of
# satellite pixels across its own footprint - real signal, but the same
# event shown many times over. A visual-dedup heuristic, not exact.
GROUND_DEDUP_RADIUS_DEG = 0.15


def _dedupe_against_ground(satellite_fires: list[dict], ground_fires: list[dict]) -> list[dict]:
    if not ground_fires:
        return satellite_fires

    # Bucketed onto a grid sized to the dedup radius, so each satellite fire
    # only checks its own cell + 8 neighbors - O(satellite + ground) instead
    # of O(satellite * ground).
    buckets: dict[tuple[int, int], list[dict]] = {}
    for gf in ground_fires:
        key = (round(gf["lat"] / GROUND_DEDUP_RADIUS_DEG), round(gf["lng"] / GROUND_DEDUP_RADIUS_DEG))
        buckets.setdefault(key, []).append(gf)

    def near_ground(f: dict) -> bool:
        cell_lat = round(f["lat"] / GROUND_DEDUP_RADIUS_DEG)
        cell_lng = round(f["lng"] / GROUND_DEDUP_RADIUS_DEG)
        for d_lat in (-1, 0, 1):
            for d_lng in (-1, 0, 1):
                for gf in buckets.get((cell_lat + d_lat, cell_lng + d_lng), []):
                    if abs(f["lat"] - gf["lat"]) <= GROUND_DEDUP_RADIUS_DEG and abs(f["lng"] - gf["lng"]) <= GROUND_DEDUP_RADIUS_DEG:
                        return True
        return False

    return [f for f in satellite_fires if not near_ground(f)]


async def _refresh_world_fires() -> None:
    global _world_fires
    results = await asyncio.gather(*(_fetch_firms_fires(WORLD_AREA, source) for source in FIRMS_SOURCES))
    satellite_fires = [f for source_fires in results for f in source_fires]

    try:
        ground_fires = await _fetch_canada_fires()
    except Exception:
        # A supplement, not the backbone - satellite data still goes out
        # if this is down or changes shape.
        ground_fires = []

    satellite_fires = _dedupe_against_ground(satellite_fires, ground_fires)
    _world_fires = satellite_fires + ground_fires
    _world_view_cache.clear()  # stale now that the underlying data changed


async def _world_fires_refresh_loop() -> None:
    # Fetches first, then sleeps - not the other way around. Run as a
    # background task from startup (not awaited there), so a slow or
    # failing NASA/CWFIS response never blocks the ASGI startup event
    # itself. That event completing is what makes the app "ready" - if
    # it never completes (FIRMS hanging, not just erroring, doesn't get
    # caught by a try/except around an await IN that event), Render has
    # nothing to route traffic to and shows "502 - service unavailable"
    # until the deploy times out, exactly like a real outage even though
    # the app itself is fine and would serve empty-but-valid responses
    # in the meantime.
    while True:
        try:
            await _refresh_world_fires()
        except Exception as e:
            print(f"[refresh] fire fetch failed, keeping last known data: {type(e).__name__}: {e}")
        await asyncio.sleep(WORLD_REFRESH_INTERVAL_SECONDS)


@app.on_event("startup")
async def _on_startup():
    global _http_client
    _http_client = httpx.AsyncClient()
    asyncio.create_task(_cache_eviction_loop())
    if not FIRMS_MAP_KEY:
        return
    asyncio.create_task(_world_fires_refresh_loop())


@app.on_event("shutdown")
async def _on_shutdown():
    if _http_client is not None:
        await _http_client.aclose()


def _filter_bbox(fires: list[dict], west: float, south: float, east: float, north: float) -> list[dict]:
    return [f for f in fires if west <= f["lng"] <= east and south <= f["lat"] <= north]


# ~orange/red color boundary the frontend uses (see intensityT/FRP_SCALE_REF
# in static/index.html - top ~8-10% most intense fires worldwide). A fire at
# or above this renders as its own marker, skipping grid grouping entirely.
# Keep in sync with the frontend's severity thresholds.
LARGE_FIRE_MIN_FRP = 27.4


def _cluster_fires(fires: list[dict], grid_deg: float) -> list[dict]:
    """Groups fires within grid_deg of each other into one FRP-weighted
    centroid marker - same aggregation as static/index.html's clusterFires(),
    run here so a dense area's response is a few hundred/thousand aggregate
    points instead of tens of thousands of raw ones. Nothing is dropped,
    unlike a top-N cap. Fires at or above LARGE_FIRE_MIN_FRP skip grouping."""
    large_fires = [f for f in fires if f["frp"] >= LARGE_FIRE_MIN_FRP]
    small_fires = [f for f in fires if f["frp"] < LARGE_FIRE_MIN_FRP]

    clusters = [
        {"lat": f["lat"], "lng": f["lng"], "count": 1, "totalFrp": f["frp"], "maxFrp": f["frp"]}
        for f in large_fires
    ]

    groups: dict[tuple[int, int], list[dict]] = {}
    for f in small_fires:
        key = (round(f["lat"] / grid_deg), round(f["lng"] / grid_deg))
        groups.setdefault(key, []).append(f)

    for group in groups.values():
        total_frp = sum(f["frp"] for f in group)
        max_frp = max(f["frp"] for f in group)
        weight = total_frp or len(group)
        lat = sum(f["lat"] * (f["frp"] or 1) for f in group) / weight
        lng = sum(f["lng"] * (f["frp"] or 1) for f in group) / weight
        clusters.append({"lat": lat, "lng": lng, "count": len(group), "totalFrp": total_frp, "maxFrp": max_frp})
    return clusters


# Bounds how many /api/fires requests can be doing the actual filter/
# cluster/encode work at once. Normal usage - even a dozen-plus different
# people doing different things simultaneously - never gets remotely close
# to this, so it never affects them. It only matters in a genuine pile-up
# (many people loading the map in the same few seconds, before the cache
# above has anything to serve yet): instead of every one of them queuing
# behind however long it takes to grind through the whole backlog
# (observed directly: minutes, with a real risk of the memory spike that
# caused the original OOM crash), requests past the cap get a fast, clear
# "busy, retry" response instead of hanging.
_FIRES_COMPUTE_SEMAPHORE = asyncio.Semaphore(20)
_FIRES_COMPUTE_WAIT_TIMEOUT_SECONDS = 15


@app.get("/api/fires")
async def get_fires(
    request: Request,
    bbox: str | None = Query(
        None, description="west,south,east,north - restricts the query to a region (e.g. one country)"
    ),
    grid: float | None = Query(
        None, gt=0, description="pre-aggregate fires into grid_deg x grid_deg FRP-weighted clusters before returning"
    ),
    min_frp: float | None = Query(
        None, description="drop individual detections weaker than this FRP (MW) - the 'toggle small fires' filter"
    ),
    min_confidence: str | None = Query(
        None, description="'n' or 'h' - additionally drop detections below this VIIRS confidence level"
    ),
    min_hectares: float | None = Query(
        None,
        description="drop ground-sourced fires smaller than this many hectares - satellite detections "
        "(no hectare data) are unaffected either way",
    ),
):
    if not FIRMS_MAP_KEY:
        raise HTTPException(
            status_code=500,
            detail="FIRMS_MAP_KEY environment variable is not set. "
            "Get a free key at https://firms.modaps.eosdis.nasa.gov/api/",
        )

    # Only the world view (no bbox) is cached - many concurrent visitors
    # requesting the exact same default view is the common case worth
    # short-circuiting; a country search's bbox varies too much per user
    # to be worth caching the same way.
    cache_key = (grid, min_frp, min_confidence, min_hectares)
    if bbox is None and cache_key in _world_view_cache:
        plain_body, gzip_body = _world_view_cache[cache_key]
        if "gzip" in request.headers.get("accept-encoding", ""):
            return Response(content=gzip_body, media_type="application/json", headers={"Content-Encoding": "gzip"})
        return Response(content=plain_body, media_type="application/json")

    try:
        await asyncio.wait_for(_FIRES_COMPUTE_SEMAPHORE.acquire(), timeout=_FIRES_COMPUTE_WAIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="Server is busy right now - please try again in a moment.")
    try:
        fires = _world_fires
        if bbox is not None:
            parts = bbox.split(",")
            if len(parts) != 4:
                raise HTTPException(status_code=400, detail="bbox must be 'west,south,east,north'")
            try:
                west, south, east, north = (float(p) for p in parts)
            except ValueError:
                raise HTTPException(status_code=400, detail="bbox values must be numeric")
            if not (-180 <= west <= 180 and -180 <= east <= 180):
                raise HTTPException(status_code=400, detail="west/east must be within [-180, 180]")
            if not (-90 <= south <= 90 and -90 <= north <= 90):
                raise HTTPException(status_code=400, detail="south/north must be within [-90, 90]")
            if west >= east or south >= north:
                raise HTTPException(status_code=400, detail="bbox must satisfy west<east and south<north")
            fires = _filter_bbox(fires, west, south, east, north)

        # min_confidence is accepted but currently ignored - an "h"-only floor
        # dropped France from 300 detections to 1 ("h" confidence is rare in
        # VIIRS data generally). Re-enable with a better-calibrated threshold.

        if min_frp is not None:
            # A flat intensity floor, not a top-N cap - a rank-based cap can
            # bury a real regional fire under stronger fires elsewhere.
            fires = [f for f in fires if f["frp"] >= min_frp]

        if min_hectares is not None:
            # Only affects ground-sourced fires (the only ones with "ha").
            fires = [f for f in fires if f.get("ha") is None or f["ha"] >= min_hectares]

        raw_count = len(fires)

        if grid is not None:
            # Aggregated, not truncated - every fire still contributes to a cluster.
            fires = _cluster_fires(fires, grid)

        result = {"fires": fires, "count": len(fires), "raw_count": raw_count}
        if bbox is None:
            # Rendered once here for future cache hits; this request's own
            # response is still produced normally below (via `return result`),
            # so a cache miss behaves identically to before this cache existed.
            plain_body = json.dumps(jsonable_encoder(result)).encode("utf-8")
            _world_view_cache[cache_key] = (plain_body, gzip.compress(plain_body, compresslevel=6))
        return result
    finally:
        _FIRES_COMPUTE_SEMAPHORE.release()


WIND_URL = "https://api.open-meteo.com/v1/forecast"


class WindPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


# Caps how many points a single request can submit - the frontend never
# sends more than ~150 (predictions) or ~750 (elevation probes, 5/cell),
# so a much higher request would only ever come from someone trying to
# use this server to hammer Open-Meteo with a huge batch.
MAX_BATCH_POINTS = 1000

# Caps how many requests to Open-Meteo can be in flight at once, across
# every endpoint and every concurrent user - a single prediction already
# fires several chunks concurrently (see FORECAST_BATCH_CHUNK_SIZE below),
# and two people predicting at the same moment used to just let all of
# those race out at once, which is exactly what tripped Open-Meteo's rate
# limit (observed directly: 429s on 24-40 point chunks in production).
# This throttles to a shared budget instead - requests queue for a slot
# rather than all firing simultaneously. Lowered from 4 to 3 after a large
# country's prediction (many chunks) still tripped 429s repeatedly even
# with the cap and retries below - a gentler outbound rate is more likely
# to stay under Open-Meteo's limit in the first place, not just recover
# from it faster.
_OPEN_METEO_CONCURRENCY = asyncio.Semaphore(3)


async def _get_with_retry(url: str, params: dict, timeout: float, max_retries: int = 4) -> httpx.Response:
    """GET through the shared client and concurrency cap, retrying a 429
    (rate limited) with backoff instead of just dropping that chunk's
    points - a 429 used to silently degrade a prediction (fewer markers
    than fires actually present) rather than being retried.

    The concurrency slot is only held for the actual request attempt, not
    the backoff sleep between attempts - holding it through the sleep (the
    original bug here) meant a chunk waiting out a 429 kept occupying one
    of the few slots, blocking other chunks from even making their FIRST
    attempt, which made a rate-limited burst worse instead of better.
    Retries/backoff were also raised (2->4 attempts, 2s->4s base, more
    generous exponential growth) after a large country's prediction (many
    chunks fired close together) kept exhausting the old 2-retry/short-
    backoff budget before Open-Meteo's rate-limit window actually reset -
    predictions already show a live elapsed-time counter, so a slower but
    complete result reads better than a faster but partial one."""
    for attempt in range(max_retries + 1):
        async with _OPEN_METEO_CONCURRENCY:
            resp = await _http_client.get(url, params=params, timeout=timeout)
        if resp.status_code != 429 or attempt == max_retries:
            resp.raise_for_status()
            return resp
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else 4.0 * (2**attempt)
        print(f"[open-meteo] 429 rate limited on {url}, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
        await asyncio.sleep(delay)


@app.post("/api/wind/batch")
async def get_wind_batch(points: list[WindPoint] = Body(..., max_length=MAX_BATCH_POINTS)):
    """Wind for many locations at once, chunked into a handful of requests
    (Open-Meteo supports comma-separated lat/lng lists) rather than one per
    point. Cached per-point since the same points get re-requested often."""
    if not points:
        return {"results": []}

    now = time.time()
    uncached_points = []
    for p in points:
        key = f"{_round_coord(p.lat)},{_round_coord(p.lng)}"
        cached = _current_wind_cache.get(key)
        if cached is None or (now - cached["fetched_at"]) >= FORECAST_CACHE_TTL_SECONDS:
            uncached_points.append(p)

    # Open-Meteo is a GET API - a long point list hits the URL length limit
    # (414), so points go out in bounded chunks. One chunk failing just
    # skips its own points rather than failing the whole request.
    WIND_BATCH_CHUNK_SIZE = 200
    for i in range(0, len(uncached_points), WIND_BATCH_CHUNK_SIZE):
        chunk = uncached_points[i : i + WIND_BATCH_CHUNK_SIZE]
        params = {
            "latitude": ",".join(str(p.lat) for p in chunk),
            "longitude": ",".join(str(p.lng) for p in chunk),
            "current": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "kmh",
        }
        try:
            resp = await _get_with_retry(WIND_URL, params, timeout=15)
            data = resp.json()
        except httpx.HTTPError as e:
            # Logged so it's visible in host logs (e.g. Render) when the
            # wind overlay/predictions come back empty in production - a
            # 200 OK in the access log alone says nothing about this.
            print(f"[wind/batch] Open-Meteo request failed: {type(e).__name__}: {e}")
            continue
        if isinstance(data, dict):  # Open-Meteo returns a plain object for a single point
            data = [data]
        for point, entry in zip(chunk, data):
            # One malformed entry shouldn't take out the rest of the batch.
            try:
                current = entry["current"]
                key = f"{_round_coord(point.lat)},{_round_coord(point.lng)}"
                _current_wind_cache[key] = {
                    "wind_speed_kmh": current["wind_speed_10m"],
                    "wind_blowing_toward_deg": (current["wind_direction_10m"] + 180) % 360,
                    "fetched_at": now,
                }
            except (KeyError, TypeError):
                continue

    results = []
    for point in points:
        key = f"{_round_coord(point.lat)},{_round_coord(point.lng)}"
        cached = _current_wind_cache.get(key)
        if cached is not None:
            results.append(
                {
                    "lat": point.lat,
                    "lng": point.lng,
                    "wind_speed_kmh": cached["wind_speed_kmh"],
                    "wind_blowing_toward_deg": cached["wind_blowing_toward_deg"],
                }
            )
    return {"results": results}


class ForecastRequest(BaseModel):
    points: list[WindPoint] = Field(max_length=MAX_BATCH_POINTS)
    hours: int = Field(ge=1, le=24 * 14)


# Per-point caches, keyed by rounded coordinates (~110m precision - plenty
# for wind/terrain). Re-running a prediction re-requests mostly the same
# cells, and Open-Meteo's free tier rate-limits fairly aggressively.
_forecast_cache: dict[str, dict] = {}  # "lat,lng,hours" -> {"steps": [...], "fetched_at": float}
_elevation_cache: dict[str, dict] = {}  # "lat,lng" -> {"elevation_m": float, "fetched_at": float}
_current_wind_cache: dict[str, dict] = {}  # "lat,lng" -> {"wind_speed_kmh", "wind_blowing_toward_deg", "fetched_at"}
FORECAST_CACHE_TTL_SECONDS = 15 * 60  # matches the FIRMS cache TTL
ELEVATION_CACHE_TTL_SECONDS = 24 * 60 * 60  # terrain doesn't change; cached long, not forever, in case of bad reads

# A cache entry going stale only ever meant "re-fetch it next time it's
# asked for" - an entry nobody asks for again just sat there forever,
# growing all three dicts without bound over the site's lifetime. This
# sweeps out anything already past its own TTL above (so cache *lifetime*
# - and the API-call savings that comes with it, especially the 24h
# elevation one - is unchanged), just on a short interval so memory is
# reclaimed promptly instead of accumulating for however long the process
# happens to stay up.
CACHE_EVICTION_INTERVAL_SECONDS = 2 * 60


def _evict_expired(cache: dict[str, dict], ttl_seconds: float, now: float) -> None:
    expired = [k for k, v in cache.items() if now - v["fetched_at"] >= ttl_seconds]
    for k in expired:
        del cache[k]


async def _cache_eviction_loop() -> None:
    while True:
        await asyncio.sleep(CACHE_EVICTION_INTERVAL_SECONDS)
        now = time.time()
        _evict_expired(_forecast_cache, FORECAST_CACHE_TTL_SECONDS, now)
        _evict_expired(_elevation_cache, ELEVATION_CACHE_TTL_SECONDS, now)
        _evict_expired(_current_wind_cache, FORECAST_CACHE_TTL_SECONDS, now)


def _round_coord(v: float) -> float:
    return round(v, 3)


@app.post("/api/wind/forecast/batch")
async def get_wind_forecast_batch(req: ForecastRequest):
    """Hourly wind + humidity + precipitation + temperature forecast for many
    locations at once, subsampled to every 6th hour to keep the response a
    reasonable size. Backs the higher prediction-strength tiers, where the
    spread path bends as the wind shifts over the projection window instead
    of assuming one static direction for the whole thing."""
    if not req.points:
        return {"results": []}

    now = time.time()
    uncached_points = []
    for p in req.points:
        key = f"{_round_coord(p.lat)},{_round_coord(p.lng)},{req.hours}"
        cached = _forecast_cache.get(key)
        if cached is None or (now - cached["fetched_at"]) >= FORECAST_CACHE_TTL_SECONDS:
            uncached_points.append(p)

    if uncached_points:
        forecast_days = min(7, max(1, (req.hours // 24) + 2))

        # A single unchunked request for a country with many fire clusters
        # (e.g. France) was slow enough on Open-Meteo's end to blow past a
        # flat 15s timeout, silently returning zero predictions for that
        # whole country. Chunked (same pattern as /api/wind/batch) and run
        # concurrently, so this isn't slower overall.
        FORECAST_BATCH_CHUNK_SIZE = 40

        async def fetch_chunk(chunk: list[WindPoint]) -> None:
            params = {
                "latitude": ",".join(str(p.lat) for p in chunk),
                "longitude": ",".join(str(p.lng) for p in chunk),
                "hourly": "wind_speed_10m,wind_direction_10m,relative_humidity_2m,precipitation,temperature_2m",
                "wind_speed_unit": "kmh",
                "forecast_days": forecast_days,
            }
            try:
                resp = await _get_with_retry(WIND_URL, params, timeout=25)
                data = resp.json()
            except httpx.HTTPError as e:
                print(f"[wind/forecast/batch] Open-Meteo request failed for a "
                      f"{len(chunk)}-point chunk: {type(e).__name__}: {e}")
                return
            if isinstance(data, dict):
                # Open-Meteo returns a single object both for a genuine
                # one-point request and for some error conditions - a real
                # forecast always has "hourly"; anything else needs logging,
                # not silently zipping against only the first point.
                if "hourly" not in data and len(chunk) != 1:
                    print(f"[wind/forecast/batch] unexpected single-object response for "
                          f"a {len(chunk)}-point chunk: {data}")
                    return
                data = [data]
            elif len(data) != len(chunk):
                print(f"[wind/forecast/batch] Open-Meteo returned {len(data)} entries "
                      f"for a {len(chunk)}-point chunk")

            skipped = 0
            for point, entry in zip(chunk, data):
                try:
                    hourly = entry["hourly"]
                    speeds = hourly["wind_speed_10m"]
                    dirs = hourly["wind_direction_10m"]
                    hums = hourly["relative_humidity_2m"]
                    precip = hourly["precipitation"]
                    temps = hourly["temperature_2m"]
                    steps = [
                        {
                            "hours_from_now": i,
                            "wind_speed_kmh": speeds[i],
                            "wind_blowing_toward_deg": (dirs[i] + 180) % 360,
                            "humidity_pct": hums[i],
                            "precipitation_mm": precip[i],
                            "temperature_c": temps[i],
                        }
                        for i in range(0, min(len(speeds), req.hours + 1), 6)
                    ]
                except (KeyError, TypeError, IndexError) as e:
                    skipped += 1
                    if skipped <= 3:  # first few only - a bad chunk can mean every point fails the same way
                        print(f"[wind/forecast/batch] skipped point ({point.lat},{point.lng}): "
                              f"{type(e).__name__}: {e} - entry was {entry!r}")
                    continue
                key = f"{_round_coord(point.lat)},{_round_coord(point.lng)},{req.hours}"
                _forecast_cache[key] = {"steps": steps, "fetched_at": now}

        chunks = [
            uncached_points[i : i + FORECAST_BATCH_CHUNK_SIZE]
            for i in range(0, len(uncached_points), FORECAST_BATCH_CHUNK_SIZE)
        ]
        await asyncio.gather(*(fetch_chunk(c) for c in chunks))

    results = []
    for point in req.points:
        key = f"{_round_coord(point.lat)},{_round_coord(point.lng)},{req.hours}"
        cached = _forecast_cache.get(key)
        if cached is not None:
            results.append({"lat": point.lat, "lng": point.lng, "steps": cached["steps"]})
    return {"results": results}


ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"


@app.post("/api/elevation/batch")
async def get_elevation_batch(points: list[WindPoint] = Body(..., max_length=MAX_BATCH_POINTS)):
    """Ground elevation for many locations in one request - used to bias
    predicted spread speed uphill/downhill at prediction strength 3+."""
    if not points:
        return {"results": []}

    now = time.time()
    uncached_points = []
    for p in points:
        key = f"{_round_coord(p.lat)},{_round_coord(p.lng)}"
        cached = _elevation_cache.get(key)
        if cached is None or (now - cached["fetched_at"]) >= ELEVATION_CACHE_TTL_SECONDS:
            uncached_points.append(p)

    if uncached_points:
        params = {
            "latitude": ",".join(str(p.lat) for p in uncached_points),
            "longitude": ",".join(str(p.lng) for p in uncached_points),
        }
        resp = await _get_with_retry(ELEVATION_URL, params, timeout=15)
        data = resp.json()
        for point, elevation_m in zip(uncached_points, data["elevation"]):
            key = f"{_round_coord(point.lat)},{_round_coord(point.lng)}"
            _elevation_cache[key] = {"elevation_m": elevation_m, "fetched_at": now}

    results = []
    for point in points:
        key = f"{_round_coord(point.lat)},{_round_coord(point.lng)}"
        cached = _elevation_cache.get(key)
        if cached is not None:
            results.append({"lat": point.lat, "lng": point.lng, "elevation_m": cached["elevation_m"]})
    return {"results": results}


GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim requires an identifying User-Agent; fine here since it's only a
# fallback, not the primary path.
NOMINATIM_HEADERS = {"User-Agent": "hacksocial26-fire-tracker/1.0 (hackathon project)"}


@app.get("/api/geocode")
async def geocode(q: str = Query(..., min_length=1, max_length=200, description="free-text place name or address to look up")):
    """Turns a place name/address into a lat/lng. Tries Open-Meteo first
    (free, no key, no documented rate limit), but it's a place-name
    gazetteer that often misses a full street address - Nominatim
    (OpenStreetMap) covers those as a second attempt."""
    params = {"name": q, "count": 1, "language": "en", "format": "json"}
    try:
        resp = await _http_client.get(GEOCODE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError:
        data = {}

    results = data.get("results") or []
    if results:
        top = results[0]
        return {
            "lat": top["latitude"],
            "lng": top["longitude"],
            "name": top.get("name", q),
            "admin1": top.get("admin1"),
            "country": top.get("country"),
            "source": "open-meteo",
        }

    try:
        resp = await _http_client.get(
            NOMINATIM_URL,
            params={"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        nominatim_results = resp.json()
    except httpx.HTTPError:
        nominatim_results = []

    if not nominatim_results:
        raise HTTPException(status_code=404, detail=f"Couldn't find a location matching '{q}'")

    top = nominatim_results[0]
    # display_name is Nominatim's full address string - unusable as a UI
    # label. jsonv2's structured `address` object builds a short
    # "street, city" name instead, matching Open-Meteo's result shape.
    addr = top.get("address") or {}
    street = " ".join(p for p in (addr.get("house_number"), addr.get("road")) if p)
    locality = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("suburb")
    short_name = ", ".join(p for p in (street, locality) if p) or top.get("display_name", q)
    return {
        "lat": float(top["lat"]),
        "lng": float(top["lon"]),
        "name": short_name,
        "admin1": addr.get("state"),
        "country": addr.get("country"),
        "source": "nominatim",
    }


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
