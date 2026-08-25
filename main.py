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
from collections import OrderedDict
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()  # picks up FIRMS_MAP_KEY from a local .env file, so it survives server restarts
from fastapi import Body, FastAPI, HTTPException, Query, Request
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
                    # str.split() hands back a BRAND NEW string object per
                    # row, so half a million rows held half a million private
                    # copies of a value drawn from three distinct confidence
                    # codes, seven dates and ~1400 timestamps. Interning
                    # collapses each set to one shared object - measured at
                    # 330 -> 198 bytes per record, 40% off the largest single
                    # allocation this server makes. See _refresh_world_fires
                    # for why that matters twice over.
                    confidence=sys.intern(cols[idx["confidence"]]),
                    frp=float(cols[idx["frp"]]),  # fire radiative power (MW), proxy for intensity
                    acq_date=sys.intern(cols[idx["acq_date"]]),
                    acq_time=sys.intern(cols[idx["acq_time"]]),
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
# scratch; this caches the rendered response so a hit skips straight to
# sending bytes - no re-clustering, re-serializing, or re-compressing.
# (Caching the compressed form, rather than relying on GZipMiddleware to
# recompress fresh each time, is what actually removes that per-request
# cost - middleware has no way to know two requests want identical output.)
#
# ONLY the gzipped bytes are kept, and only a handful of entries. The first
# version stored the plain copy alongside the compressed one and had no
# entry limit, which caused an out-of-memory restart in production: the UI
# can produce 28 distinct keys (7 detail levels x 2 filter toggles x 2), and
# holding a full uncompressed world response for each measured at +90MB on a
# local dataset - several times that on the real one. Compressed-only is
# ~4-5x smaller, and the cap bounds it regardless of how many combinations
# get browsed between refreshes. Also cleared whenever _world_fires
# refreshes (see _refresh_world_fires), so it's never staler than the data.
_WORLD_VIEW_CACHE_MAX_ENTRIES = 8
_world_view_cache: "OrderedDict[tuple, bytes]" = OrderedDict()  # key -> gzipped json


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
    # The new dataset is fully built before it replaces the old one, so both
    # are resident for an instant and this line is the high-water mark of the
    # whole process - roughly double the steady state, every refresh. That is
    # deliberate: the alternative is a window where requests see no fires at
    # all. It's also why the per-record size above is worth the attention -
    # every byte saved there is saved twice here.
    _world_fires = satellite_fires + ground_fires
    _world_view_cache.clear()  # stale now that the underlying data changed
    await _warm_world_view_cache()


# What the frontend asks for on a first visit, with nothing touched: the
# Detail level slider's default grid, "show small fires" off, and ground
# fires unfiltered. Kept in sync with static/index.html -
# DETAIL_GRID_STEPS[DETAIL_DEFAULT_INDEX], MIN_FRP_THRESHOLD,
# currentMinConfidence(), currentMinHectares(), FIRE_ESTIMATE_GRID.
# If the slider's default grid ever changes, change it here too or every
# first page load silently goes back to being a cold build.
_DEFAULT_VIEW_GRID = 0.5
_DEFAULT_VIEW_MIN_FRP = 3.0
_DEFAULT_VIEW_MIN_CONFIDENCE = "h"
_ESTIMATE_GRID = 0.05


async def _warm_world_view_cache() -> None:
    """Builds the two responses a first-time visitor always requests, so the
    first person through the door doesn't pay for them.

    Everything about a cold world view is expensive - filtering 455,000
    detections, clustering them, encoding and compressing the result - and
    all of it was landing on whoever happened to arrive first after a data
    refresh, measured at 29 seconds while the map sat on a loading screen.
    None of that work depends on the request, so it's done here on the
    refresh timer instead, off the request path entirely. A visitor arriving
    afterwards gets a cache hit measured in milliseconds.

    Only these two entries are warmed. Every other combination of the
    filter switches is a deliberate action by someone already looking at a
    working map, who can afford to wait a moment for it."""
    warm = [
        # The map itself - this is what the loading screen is waiting on.
        (_DEFAULT_VIEW_GRID, False),
        # The "~N active fires worldwide" headline, which is a separate
        # request at a much finer grid and used to be the more expensive of
        # the two by a wide margin.
        (_ESTIMATE_GRID, True),
    ]
    for grid, count_only in warm:
        key = (grid, _DEFAULT_VIEW_MIN_FRP, _DEFAULT_VIEW_MIN_CONFIDENCE, None, count_only)
        try:
            body = await asyncio.to_thread(
                _build_fires_payload, _world_fires, grid, _DEFAULT_VIEW_MIN_FRP, None, count_only)
        except Exception as e:
            # Warming is an optimisation, never a requirement - a failure
            # here just means the next visitor builds it themselves.
            print(f"[warm] failed to pre-build grid={grid} count_only={count_only}: {type(e).__name__}: {e}")
            continue
        _world_view_cache[key] = body
        _world_view_cache.move_to_end(key)
    while len(_world_view_cache) > _WORLD_VIEW_CACHE_MAX_ENTRIES:
        _world_view_cache.popitem(last=False)


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


def _cluster_fires(fires: list[dict], grid_deg: float, large_fire_min_frp: float = LARGE_FIRE_MIN_FRP) -> list[dict]:
    """Groups fires within grid_deg of each other into one FRP-weighted
    centroid marker - same aggregation as static/index.html's clusterFires(),
    run here so a dense area's response is a few hundred/thousand aggregate
    points instead of tens of thousands of raw ones. Nothing is dropped,
    unlike a top-N cap. Fires at or above large_fire_min_frp skip grouping.

    That exemption is right for the live map - a major fire shouldn't be
    visually diluted by small ones beside it - but it means the output size
    is driven by however many intense detections there are, not by the grid.
    The history endpoint passes infinity to disable it, because a week of a
    continent is tens of thousands of above-threshold detections per day,
    which is both unreadable at that zoom and megabytes of JSON."""
    large_fires = [f for f in fires if f["frp"] >= large_fire_min_frp]
    small_fires = [f for f in fires if f["frp"] < large_fire_min_frp]

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


def _gzip_response(gzip_body: bytes, request: Request) -> Response:
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(content=gzip_body, media_type="application/json", headers={"Content-Encoding": "gzip"})
    # Every real browser accepts gzip, so this path is rare enough that
    # decompressing beats keeping a second uncompressed copy in memory.
    return Response(content=gzip.decompress(gzip_body), media_type="application/json")


def _fire_to_dict(f) -> dict:
    return {
        "lat": f.lat, "lng": f.lng, "confidence": f.confidence, "frp": f.frp,
        "acq_date": f.acq_date, "acq_time": f.acq_time, "ha": f.ha,
    }


def _build_fires_payload(fires, grid, min_frp, min_hectares, count_only) -> bytes:
    """Filter, cluster, encode and compress, in one blocking call.

    Deliberately one function rather than steps spread through the request
    handler, because the cache warmer (see _warm_world_view_cache) has to
    produce byte-for-byte the same thing the handler would - if the two ever
    drifted, the warmer would poison the cache with subtly different content
    under a key the handler thinks it owns. Sharing the code makes that
    impossible rather than merely unlikely.

    min_confidence is accepted by the endpoint but not applied here - an
    "h"-only floor dropped France from 300 detections to 1 ("h" confidence is
    rare in VIIRS data generally). It stays in the cache key so the frontend
    can keep sending it. Re-enable with a better-calibrated threshold.
    """
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

    # count_only skips the fire list entirely. The headline "~N active fires
    # worldwide" figure needs the number and nothing else, but it used to
    # serialize and compress all 86,000 clusters to deliver it - measured at
    # 18 of the 22 seconds that request cost, and it runs concurrently with
    # the map's own load, starving it of CPU. The clustering still happens
    # (the count IS the cluster count), just not the encoding of a list
    # nobody reads.
    result = (
        {"count": len(fires), "raw_count": raw_count}
        if count_only
        else {"fires": fires, "count": len(fires), "raw_count": raw_count}
    )
    # Encoded and compressed once, and that same buffer is both cached and
    # returned. Previously a cache miss did this twice - by hand for the
    # cache, then again by FastAPI and GZipMiddleware for the response - so
    # the slowest path was paying double.
    return gzip.compress(_encode_fires_payload(result), compresslevel=6)


def _encode_fires_payload(result: dict) -> bytes:
    """JSON bytes for a /api/fires response, without FastAPI's jsonable_encoder.

    That encoder walks every value recursively to work out how to serialize
    it, which is the right general-purpose behaviour and completely wasted
    here: clustered output is already plain dicts of floats and ints, and
    raw output is a list of one known dataclass. Measured on the real world
    dataset, jsonable_encoder took 12.8s where a direct json.dumps took 3.3s
    for the same payload - it was the single most expensive step in a cold
    request, costing about 4x what the encoding actually requires."""
    fires = result.get("fires")
    if fires and isinstance(fires[0], Fire):
        result = {**result, "fires": [_fire_to_dict(f) for f in fires]}
    return json.dumps(result).encode("utf-8")


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
    count_only: bool = Query(
        False,
        description="return just the counts, skipping the fire list entirely - for the headline "
        "'~N active fires worldwide' figure, which needs the number and nothing else",
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
    # The refresh loop runs as a background task so a slow NASA response can
    # never block startup (see _world_fires_refresh_loop), which means the
    # very first visitors after a deploy or a cold start can arrive before
    # any data exists. Answering those with a normal empty result would look
    # identical to "the world is not on fire"; this says which it is, so the
    # frontend can show that it's still coming and retry rather than
    # rendering a convincingly empty globe.
    if not _world_fires:
        return {"fires": [], "count": 0, "raw_count": 0, "ready": False}

    cache_key = (grid, min_frp, min_confidence, min_hectares, count_only)
    if bbox is None and cache_key in _world_view_cache:
        gzip_body = _world_view_cache[cache_key]
        _world_view_cache.move_to_end(cache_key)  # LRU: most recently used stays longest
        return _gzip_response(gzip_body, request)

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

        # Everything from here is pure synchronous Python over hundreds of
        # thousands of fires, so it all goes onto a worker thread together.
        # Running any of it inline was found (during a load-testing pass) to
        # stall every OTHER concurrent request - even cheap cache hits and
        # unrelated endpoints - for as long as it took, since nothing else on
        # the single event loop could run meanwhile.
        gzip_body = await asyncio.to_thread(
            _build_fires_payload, fires, grid, min_frp, min_hectares, count_only)
        if bbox is None:
            _world_view_cache[cache_key] = gzip_body
            _world_view_cache.move_to_end(cache_key)
            while len(_world_view_cache) > _WORLD_VIEW_CACHE_MAX_ENTRIES:
                _world_view_cache.popitem(last=False)  # drop least recently used
        return _gzip_response(gzip_body, request)
    finally:
        _FIRES_COMPUTE_SEMAPHORE.release()


HISTORY_MAX_DAYS = 7
# FIRMS' area endpoint takes a day range plus an optional start date. The
# range is capped at 5 by the API itself ("Invalid day range. Expects [1..5]"
# - confirmed by asking it for 7), and a start date makes the range run
# FORWARD from that date, so a 7-day window is covered by two chunks rather
# than one request. Every row carries its own acq_date, so the days are
# separated here instead of paying a request per day.
HISTORY_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/{days}/{start}"
HISTORY_MAX_DAYS_PER_REQUEST = 5


# CWFIS's reported-fires layer is a genuine time series: every record carries
# a record_start/record_end validity window, so asking which records were
# valid at a given instant returns the ground-confirmed picture AS IT STOOD
# then, not today's list filtered by a start date.
#
# This matters most exactly where the satellite replay looks worst. A large,
# well-established fire generates enough smoke to hide its own hottest core
# from a thermal sensor looking down at it, so Canada's biggest fires are
# often the ones MISSING from a satellite-only replay - the animation quietly
# understates the fires it should be shouting about. Ground reporting doesn't
# have that blind spot.
CWFIS_HISTORY_URL = "https://geoserver.cwfif.nrcan.gc.ca/geoserver/wfs"
CWFIS_HISTORY_PROPERTIES = "national_fire_id,latitude,longitude,fire_size,stage_of_control_status,record_start,record_end"
# Rough bounds of Canada. Outside these there is nothing for CWFIS to add, so
# the request is skipped entirely rather than paying for an empty answer.
CANADA_BOUNDS = (-141.0, 41.0, -52.0, 84.0)  # west, south, east, north


def _bbox_intersects_canada(west: float, south: float, east: float, north: float) -> bool:
    cw, cs, ce, cn = CANADA_BOUNDS
    return not (east < cw or west > ce or north < cs or south > cn)


def _parse_cwfis_time(value: str) -> datetime:
    """CWFIS timestamps come back as bare local-looking ISO strings with no
    zone marker at all ('2026-08-17T23:45:00'), which parse to naive datetimes
    and then blow up the moment they meet an aware one. They are UTC, so
    that's attached explicitly here rather than left to chance."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _fetch_canada_history(start_date, end_date) -> list[dict]:
    """Every ground-confirmed active-fire record whose validity window touches
    the requested date range, in ONE request. Bucketing the records into days
    happens locally afterwards (see _canada_fires_on_day).

    One request rather than one per day: verified against per-day queries for
    a full 7-day window and it reproduces them exactly, 0 missing and 0 extra
    on every day, for a seventh of the requests."""
    cql = (
        f"record_end >= {start_date}T00:00:00Z AND record_start <= {end_date}T23:59:59Z "
        f"AND stage_of_control_status IN ('OC','UC','BH','BM')"
    )
    params = {
        "service": "WFS", "version": "2.0.1", "request": "GetFeature",
        "outputFormat": "csv", "typeName": "public:cwfif_national_reportedfires",
        "propertyName": CWFIS_HISTORY_PROPERTIES, "CQL_FILTER": cql,
    }
    resp = await _http_client.get(CWFIS_HISTORY_URL, params=params, timeout=90)
    resp.raise_for_status()

    lines = resp.text.strip().splitlines()
    if len(lines) < 2:
        return []
    idx = {name: i for i, name in enumerate(lines[0].split(","))}

    records = []
    for line in lines[1:]:
        cols = line.split(",")
        try:
            records.append(
                {
                    "lat": float(cols[idx["latitude"]]),
                    "lng": float(cols[idx["longitude"]]),
                    # -1 is CWFIS's "size not known yet" sentinel, not a real area.
                    "ha": max(0.0, float(cols[idx["fire_size"]] or 0)),
                    "start": _parse_cwfis_time(cols[idx["record_start"]]),
                    "end": _parse_cwfis_time(cols[idx["record_end"]]),
                }
            )
        except (TypeError, ValueError, IndexError, KeyError):
            continue  # one bad row shouldn't drop the rest
    return records


def _canada_fires_on_day(records: list[dict], day) -> list[Fire]:
    """The ground-confirmed fires burning on `day`, as Fire objects.

    Sampled at noon UTC rather than over the whole day: records are short
    status snapshots, so a fire has exactly one record covering any given
    instant but many across a day, and taking the whole day would count the
    same fire several times over."""
    noon = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
    day_str = day.isoformat()
    out = []
    for r in records:
        if r["start"] <= noon <= r["end"]:
            out.append(
                Fire(
                    lat=r["lat"], lng=r["lng"],
                    confidence="h",  # agency-reported, the highest tier this app has
                    # Same hectares-based FRP proxy the live feed uses, so a
                    # ground fire carries comparable weight in a cluster
                    # whether you're looking at today or at last Tuesday.
                    frp=max(30.0, math.log1p(r["ha"]) * 15),
                    acq_date=day_str,
                    acq_time="1200",
                    ha=r["ha"],
                )
            )
    return out


def _history_chunks(days: int) -> list[tuple[str, int]]:
    """The (start_date, day_count) pairs covering the last `days` days up to
    and including today, none longer than the API allows and none
    overlapping (so nothing is double counted when the days are regrouped)."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    chunks = []
    remaining = days
    while remaining > 0:
        span = min(HISTORY_MAX_DAYS_PER_REQUEST, remaining)
        chunks.append((start.isoformat(), span))
        start += timedelta(days=span)
        remaining -= span
    return chunks

# A week of detections over a large area is a lot of rows, and this box has
# 512MB total with the live world dataset already resident. Rather than
# refusing an oversized request outright - which would mean no history at all
# for exactly the countries that need it most, since Russia's bounding box
# alone is ~40x360 degrees - the box is shrunk around its own centre and the
# response says so, so the animation still works over the part being looked at.
HISTORY_MAX_SPAN_DEG = 60.0

# FIRMS is slow (seconds per request) and a week of data is far more
# expensive to assemble than the live view, so results are cached. Keyed on
# the exact request, and short-lived because the most recent day keeps
# filling in as satellites pass over.
#
# Stores the GZIPPED RESPONSE BYTES, not the assembled Python payload, for
# both of the reasons the world-view cache learned the hard way: holding the
# decoded object for a continent-sized week (430,000 detections was measured
# for Russia) pushed RSS to 543MB, past Render's 512MB ceiling; and a "cache
# hit" that still has to re-encode and re-compress that object took 11
# seconds, which is most of the cost the cache existed to avoid. Compressed
# bytes are a few hundred KB and a hit is a straight write to the socket.
HISTORY_CACHE_TTL_SECONDS = 30 * 60
_HISTORY_CACHE_MAX_ENTRIES = 4
_history_cache: "OrderedDict[tuple, dict]" = OrderedDict()  # key -> {"gzip": bytes, "fetched_at": float}

# At a 60-degree span the camera is far enough back that sub-degree detail
# isn't visible anyway, so an over-fine grid there spends megabytes of JSON
# drawing dots on top of each other. Coarsens the clustering for large areas
# only - a country-sized request is well under this and keeps what it asked for.
HISTORY_MIN_CELLS_ACROSS = 150


def _history_response(gzip_body: bytes, request: Request) -> Response:
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(content=gzip_body, media_type="application/json", headers={"Content-Encoding": "gzip"})
    return Response(content=gzip.decompress(gzip_body), media_type="application/json")

# Bounds concurrent history builds independently of the main fires
# semaphore: these are by far the memory-heaviest thing this server does
# (half a million parsed CSV rows for a continent-week), and a pile-up of
# them is exactly the shape that caused an OOM restart before. Serialized to
# one, because two continent-sized builds overlapping doubles the worst-case
# transient footprint on a 512MB box - and this is a deliberate, occasional
# click, not something every page load triggers.
_HISTORY_SEMAPHORE = asyncio.Semaphore(1)


def _clip_bbox_span(west: float, south: float, east: float, north: float):
    """Shrinks an oversized box around its own centre, returning the box and
    whether anything was actually clipped."""
    clipped = False
    if east - west > HISTORY_MAX_SPAN_DEG:
        centre = (west + east) / 2
        west, east = centre - HISTORY_MAX_SPAN_DEG / 2, centre + HISTORY_MAX_SPAN_DEG / 2
        clipped = True
    if north - south > HISTORY_MAX_SPAN_DEG:
        centre = (south + north) / 2
        south, north = centre - HISTORY_MAX_SPAN_DEG / 2, centre + HISTORY_MAX_SPAN_DEG / 2
        clipped = True
    return max(-180.0, west), max(-90.0, south), min(180.0, east), min(90.0, north), clipped


async def _fetch_firms_history(area: str, source: str, days: int, start: str) -> list[Fire]:
    url = HISTORY_URL.format(key=FIRMS_MAP_KEY, source=source, area=area, days=days, start=start)
    resp = await _http_client.get(url, timeout=60)
    resp.raise_for_status()

    lines = resp.text.strip().splitlines()
    if not lines:
        return []
    idx = {name: i for i, name in enumerate(lines[0].split(","))}

    fires = []
    for line in lines[1:]:
        cols = line.split(",")
        try:
            fires.append(
                Fire(
                    lat=float(cols[idx["latitude"]]),
                    lng=float(cols[idx["longitude"]]),
                    # Interned for the same reason as the live feed above -
                    # a week of history is the biggest transient allocation
                    # here, and it lands while the world dataset is resident.
                    confidence=sys.intern(cols[idx["confidence"]]),
                    frp=float(cols[idx["frp"]]),
                    acq_date=sys.intern(cols[idx["acq_date"]]),
                    acq_time=sys.intern(cols[idx["acq_time"]]),
                )
            )
        except (ValueError, IndexError, KeyError):
            continue
    return fires


def _round_history_clusters(clusters: list[dict]) -> list[dict]:
    """Trims float precision in the clustered output. A cluster centroid is
    serialized as ~17 significant digits by default, which is absurd for a
    point that represents a grid cell tens of kilometres across - and across
    a week of a dense region that alone was most of a multi-megabyte
    response. 3 decimal places is ~110m, far finer than any grid used here,
    so nothing visible is lost."""
    for c in clusters:
        c["lat"] = round(c["lat"], 3)
        c["lng"] = round(c["lng"], 3)
        c["totalFrp"] = round(c["totalFrp"], 1)
        c["maxFrp"] = round(c["maxFrp"], 1)
    return clusters


def _group_history_by_day(fires: list[Fire], grid: float, ground_by_day: dict | None = None) -> list[dict]:
    by_date: dict[str, list[Fire]] = {}
    for f in fires:
        by_date.setdefault(f.acq_date, []).append(f)

    # Ground-confirmed fires can exist on a day the satellites saw nothing at
    # all, so their dates seed the set rather than only annotating days that
    # already have satellite data.
    for date in (ground_by_day or {}):
        by_date.setdefault(date, [])

    out = []
    for date, day_fires in sorted(by_date.items()):
        ground = (ground_by_day or {}).get(date, [])
        if ground:
            # Same de-duplication the live map applies: a big fire complex
            # would otherwise appear as one ground marker PLUS the scatter of
            # satellite pixels across its own footprint, inflating the day's
            # totals for the exact fires this is meant to represent properly.
            day_fires = _dedupe_against_ground(day_fires, ground) + ground
        out.append(
            # math.inf: cluster everything, including intense detections - see
            # _cluster_fires. A history frame is about where activity WAS, and
            # at a week/continent scale individual hotspots are neither
            # legible nor affordable.
            {
                "date": date,
                "fires": _round_history_clusters(_cluster_fires(day_fires, grid, math.inf)),
                "raw_count": len(day_fires),
                "ground_count": len(ground),
            }
        )
    return out


@app.get("/api/fires/history")
async def get_fires_history(
    request: Request,
    bbox: str = Query(..., description="west,south,east,north - required; a whole-world history is far too much data"),
    days: int = Query(HISTORY_MAX_DAYS, ge=2, le=HISTORY_MAX_DAYS),
    grid: float = Query(0.15, gt=0, description="clustering grid, applied per day"),
):
    """The last few days of detections in one region, split by day, so the
    frontend can animate a fire season developing rather than only showing
    the current snapshot. A single detection is a dot; a week of them played
    in sequence is the direction a fire is actually moving.

    Unlike /api/fires this is NOT served from the in-memory world dataset -
    that only holds the most recent 2 days, which is what the live map needs.
    This goes back to FIRMS for the longer window, which is why it's bbox-only
    and cached."""
    if not FIRMS_MAP_KEY:
        raise HTTPException(status_code=500, detail="FIRMS_MAP_KEY environment variable is not set.")

    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox must be 'west,south,east,north'")
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox values must be numeric")
    if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
        raise HTTPException(status_code=400, detail="bbox is out of range")
    if west >= east or south >= north:
        raise HTTPException(status_code=400, detail="bbox must satisfy west<east and south<north")

    west, south, east, north, clipped = _clip_bbox_span(west, south, east, north)
    # Rounded before it becomes a cache key: the frontend derives this box
    # from the live camera, so two views of the same place would otherwise
    # differ in the sixth decimal and never share a cache entry.
    area = ",".join(f"{v:.2f}" for v in (west, south, east, north))
    grid = max(grid, max(east - west, north - south) / HISTORY_MIN_CELLS_ACROSS)
    cache_key = (area, days, grid)

    cached = _history_cache.get(cache_key)
    if cached is not None and (time.time() - cached["fetched_at"]) < HISTORY_CACHE_TTL_SECONDS:
        _history_cache.move_to_end(cache_key)
        return _history_response(cached["gzip"], request)

    try:
        await asyncio.wait_for(_HISTORY_SEMAPHORE.acquire(), timeout=_FIRES_COMPUTE_WAIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="Server is busy right now - please try again in a moment.")
    try:
        window = [
            (datetime.now(timezone.utc) - timedelta(days=n)).date()
            for n in range(days - 1, -1, -1)
        ]
        wants_ground = _bbox_intersects_canada(west, south, east, north)

        async def canada_history():
            if not wants_ground:
                return []
            return await _fetch_canada_history(window[0], window[-1])

        results = await asyncio.gather(
            *(
                _fetch_firms_history(area, source, span, start)
                for source in FIRMS_SOURCES
                for start, span in _history_chunks(days)
            ),
            canada_history(),
            return_exceptions=True,
        )
        *satellite_results, ground_result = results

        fires = []
        for r in satellite_results:
            if isinstance(r, Exception):
                # One satellite (or one chunk of the window) failing still
                # leaves a usable animation from the rest, which is much
                # better than no history at all.
                print(f"[history] one FIRMS request failed: {type(r).__name__}: {r}")
                continue
            fires.extend(r)

        ground_by_day: dict[str, list[Fire]] = {}
        if isinstance(ground_result, Exception):
            # A supplement, not the backbone - the satellite animation still
            # goes out if CWFIS is down or changes shape.
            print(f"[history] CWFIS request failed: {type(ground_result).__name__}: {ground_result}")
        elif ground_result:
            for day in window:
                in_box = [
                    f for f in _canada_fires_on_day(ground_result, day)
                    if west <= f.lng <= east and south <= f.lat <= north
                ]
                if in_box:
                    ground_by_day[day.isoformat()] = in_box

        if not fires and not ground_by_day:
            raise HTTPException(status_code=502, detail="No history available for this area right now.")

        # Same reasoning as the live endpoint: grouping and clustering a week
        # of detections is pure synchronous Python heavy enough to stall every
        # other request on the event loop if run inline.
        days_out = await asyncio.to_thread(_group_history_by_day, fires, grid, ground_by_day)
        payload = {
            "days": days_out,
            "bbox": {"west": west, "south": south, "east": east, "north": north},
            "clipped": clipped,
            # Lets the frontend say so when ground data is contributing, since
            # it's the difference between a believable Canadian replay and one
            # that's mostly missing its biggest fires.
            "has_ground_data": bool(ground_by_day),
        }
        # Encoded and compressed off the event loop too - for a week of a
        # continent this is seconds of pure CPU, and doing it inline would
        # stall every other request for that whole time.
        # Plain dicts of primitives by this point, so jsonable_encoder's
        # recursive type walk is pure overhead here too - it measured ~4x the
        # cost of a direct dumps on the fires endpoint.
        gzip_body = await asyncio.to_thread(
            lambda: gzip.compress(json.dumps(payload).encode("utf-8"), compresslevel=6)
        )
        # `payload` and `days_out` go out of scope here; only the compressed
        # bytes are retained, which is what keeps this cache small.
        _history_cache[cache_key] = {"gzip": gzip_body, "fetched_at": time.time()}
        _history_cache.move_to_end(cache_key)
        while len(_history_cache) > _HISTORY_CACHE_MAX_ENTRIES:
            _history_cache.popitem(last=False)
        return _history_response(gzip_body, request)
    finally:
        _HISTORY_SEMAPHORE.release()


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
# every endpoint and every concurrent user. Serialized to 1: escalating
# this (previously 3, briefly 4) plus retries with growing exponential
# backoff was tried and made things WORSE, not better - a live production
# log for a France prediction showed retries backing off all the way out
# to ~90s cumulative and STILL getting 429'd, meaning the throttle wasn't
# a brief burst that backing off recovers from, it was sustained. Every
# retry in that state is just more load stacked onto an already-saturated
# shared quota (shared across every concurrent user of this app, since
# Open-Meteo rate-limits by source IP) - it doesn't help that request
# succeed and it makes the throttle last longer for everyone else's
# requests too. Fully serializing to one outbound call at a time is the
# gentlest possible request rate this app can offer, which is the actual
# lever that keeps 429s from happening in the first place.
_OPEN_METEO_CONCURRENCY = asyncio.Semaphore(1)


async def _get_with_retry(url: str, params: dict, timeout: float, max_retries: int = 2) -> httpx.Response:
    """GET through the shared client and concurrency cap, retrying a 429
    (rate limited) a couple of times with a short flat delay before giving
    up and letting the caller skip that chunk (fewer markers than fires
    actually present, logged - not a full prediction failure).

    Deliberately NOT the aggressive exponential-backoff retry (up to 4
    attempts, up to ~90s) this used to be: that assumed a 429 meant a
    brief burst that waiting out would clear, but production showed a
    sustained throttle that never cleared no matter how long the backoff
    grew - by the time attempt 4 fired (~90s in), it still 429'd. A short
    retry budget covers the case that's actually recoverable (a genuine
    momentary blip) without holding the request open for a minute and a
    half only to fail anyway.

    The concurrency slot is only held for the actual request attempt, not
    the backoff sleep between attempts - holding it through the sleep (an
    earlier bug here) meant a chunk waiting out a 429 kept occupying the
    slot, blocking other chunks from even making their FIRST attempt."""
    for attempt in range(max_retries + 1):
        async with _OPEN_METEO_CONCURRENCY:
            resp = await _http_client.get(url, params=params, timeout=timeout)
        if resp.status_code != 429 or attempt == max_retries:
            resp.raise_for_status()
            return resp
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else random.uniform(3.0, 6.0)
        print(f"[open-meteo] 429 rate limited on {url}, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
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
        cached = _current_wind_cache.get(key) or _any_cached_current_wind()
        results.append(
            {
                "lat": point.lat,
                "lng": point.lng,
                "wind_speed_kmh": cached["wind_speed_kmh"] if cached else _SYNTHETIC_WIND_SPEED_KMH,
                "wind_blowing_toward_deg": cached["wind_blowing_toward_deg"] if cached else _SYNTHETIC_WIND_BLOWING_TOWARD_DEG,
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
        _evict_expired(_air_quality_cache, AIR_QUALITY_CACHE_TTL_SECONDS, now)


def _round_coord(v: float) -> float:
    return round(v, 3)


# Absolute last resort, used only when NOTHING - not this request, not any
# other still-fresh cache entry from any other request - has a real Open-
# Meteo reading to fall back to. Observed directly in production: Render's
# shared outbound IP can be saturated badly enough that literally every
# chunk in a request 429s, leaving nothing real anywhere to borrow from.
# A flat, plausible placeholder keeps predictions/the wind overlay
# rendering something reasonable instead of coming back empty - it's not
# real weather, but a moderate breeze is a better default than a blank map.
_SYNTHETIC_WIND_SPEED_KMH = 12.0
_SYNTHETIC_WIND_BLOWING_TOWARD_DEG = 225.0
_SYNTHETIC_HUMIDITY_PCT = 45.0
_SYNTHETIC_PRECIPITATION_MM = 0.0
_SYNTHETIC_TEMPERATURE_C = 22.0


def _any_cached_current_wind() -> dict | None:
    """Any still-fresh current-wind reading, from any point, fetched by any
    request - preferred over the synthetic default when available, since a
    real reading from elsewhere at least reflects the actual weather
    pattern happening right now, just not at this exact spot."""
    return next(iter(_current_wind_cache.values()), None)


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
        # whole country. Chunked (same pattern as /api/wind/batch) so no
        # single request risks that timeout - chunks are launched together
        # but actually reach Open-Meteo one at a time, serialized by
        # _OPEN_METEO_CONCURRENCY, which trades some speed for not
        # tripping the rate limit in the first place.
        FORECAST_BATCH_CHUNK_SIZE = 40

        async def fetch_chunk(chunk: list[WindPoint]) -> None:
            # No manual staggering needed here - _OPEN_METEO_CONCURRENCY is
            # serialized to 1, so chunks are already forced through
            # one-at-a-time by the concurrency gate itself.
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

    # A chunk can still fail after retries - not from this app's own request
    # rate (Open-Meteo's real free-tier limits are far above what a single
    # prediction generates), but because Render's free tier shares outbound
    # IPs across unrelated customers, and Open-Meteo rate-limits by IP: a
    # 429 here can mean some OTHER Render app on the same shared IP is
    # using up the budget, which no amount of pacing on our end fixes.
    # Rather than dropping those points (a visibly incomplete prediction,
    # or - if every chunk in the request failed - no prediction at all),
    # borrow the nearest point's real forecast that DID succeed. It's an
    # approximation (wind can genuinely vary over a country), but a nearby
    # real reading is a far better stand-in than either an empty result or
    # a request that fails outright, and it costs zero extra Open-Meteo
    # calls, so it can't make the underlying rate-limit situation worse.
    fetched = [
        (p, _forecast_cache[f"{_round_coord(p.lat)},{_round_coord(p.lng)},{req.hours}"]["steps"])
        for p in req.points
        if f"{_round_coord(p.lat)},{_round_coord(p.lng)},{req.hours}" in _forecast_cache
    ]

    def _synthetic_steps() -> list[dict]:
        # Nothing in this batch succeeded either - fall back to any other
        # still-fresh current-wind reading (from an unrelated request) if
        # one exists, and only resort to the flat synthetic constants if
        # truly nothing real is available anywhere.
        wind = _any_cached_current_wind()
        speed = wind["wind_speed_kmh"] if wind else _SYNTHETIC_WIND_SPEED_KMH
        direction = wind["wind_blowing_toward_deg"] if wind else _SYNTHETIC_WIND_BLOWING_TOWARD_DEG
        return [
            {
                "hours_from_now": i,
                "wind_speed_kmh": speed,
                "wind_blowing_toward_deg": direction,
                "humidity_pct": _SYNTHETIC_HUMIDITY_PCT,
                "precipitation_mm": _SYNTHETIC_PRECIPITATION_MM,
                "temperature_c": _SYNTHETIC_TEMPERATURE_C,
            }
            for i in range(0, req.hours + 1, 6)
        ]

    def _fallback_steps(point: WindPoint) -> list[dict]:
        if fetched:
            return min(fetched, key=lambda fp: (fp[0].lat - point.lat) ** 2 + (fp[0].lng - point.lng) ** 2)[1]
        return _synthetic_steps()

    results = []
    for point in req.points:
        key = f"{_round_coord(point.lat)},{_round_coord(point.lng)},{req.hours}"
        cached = _forecast_cache.get(key)
        steps = cached["steps"] if cached is not None else _fallback_steps(point)
        results.append({"lat": point.lat, "lng": point.lng, "steps": steps})
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


AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
# Air quality is published hourly, so a shorter TTL would only re-fetch an
# identical reading. Longer than the wind TTL for the same reason.
AIR_QUALITY_CACHE_TTL_SECONDS = 30 * 60
_air_quality_cache: dict[str, dict] = {}  # "lat,lng" -> {"pm2_5", "us_aqi", "fetched_at"}


@app.post("/api/air-quality/batch")
async def get_air_quality_batch(points: list[WindPoint] = Body(..., max_length=MAX_BATCH_POINTS)):
    """Ground-level PM2.5 and US AQI for many locations in one request.

    Backs the smoke half of the risk picture. Fire proximity alone badly
    understates who a wildfire actually affects: the overwhelming majority
    of people harmed by one never see flame, they breathe the smoke, which
    travels hundreds of kilometres downwind. Somebody 200km from the nearest
    detection can be in genuinely hazardous air while every distance-based
    check this app does says "safe".

    Unlike the wind endpoints, this has NO synthetic fallback. A made-up
    wind direction only misaims a drawing on a globe; a made-up air quality
    number is a health figure someone might act on, and inventing one is
    worse than admitting we don't know. Points we couldn't fetch come back
    with nulls, and the frontend omits the line entirely rather than
    guessing."""
    if not points:
        return {"results": []}

    now = time.time()
    uncached_points = [
        p for p in points
        if (cached := _air_quality_cache.get(f"{_round_coord(p.lat)},{_round_coord(p.lng)}")) is None
        or (now - cached["fetched_at"]) >= AIR_QUALITY_CACHE_TTL_SECONDS
    ]

    # Same chunking rationale as /api/wind/batch: this is a GET API and a
    # long comma-separated coordinate list runs into the URL length limit.
    AIR_QUALITY_CHUNK_SIZE = 100
    for i in range(0, len(uncached_points), AIR_QUALITY_CHUNK_SIZE):
        chunk = uncached_points[i : i + AIR_QUALITY_CHUNK_SIZE]
        params = {
            "latitude": ",".join(str(p.lat) for p in chunk),
            "longitude": ",".join(str(p.lng) for p in chunk),
            "current": "pm2_5,us_aqi",
        }
        try:
            # Shares _OPEN_METEO_CONCURRENCY via _get_with_retry even though
            # it's a different Open-Meteo host: the rate limit that actually
            # bites here is per-source-IP (see the notes on that semaphore),
            # which is shared across all of their endpoints regardless.
            resp = await _get_with_retry(AIR_QUALITY_URL, params, timeout=15)
            data = resp.json()
        except httpx.HTTPError as e:
            print(f"[air-quality/batch] Open-Meteo request failed: {type(e).__name__}: {e}")
            continue
        if isinstance(data, dict):
            data = [data]
        for point, entry in zip(chunk, data):
            try:
                current = entry["current"]
            except (KeyError, TypeError):
                continue
            _air_quality_cache[f"{_round_coord(point.lat)},{_round_coord(point.lng)}"] = {
                "pm2_5": current.get("pm2_5"),
                "us_aqi": current.get("us_aqi"),
                "fetched_at": now,
            }

    results = []
    for point in points:
        cached = _air_quality_cache.get(f"{_round_coord(point.lat)},{_round_coord(point.lng)}")
        results.append(
            {
                "lat": point.lat,
                "lng": point.lng,
                "pm2_5": cached["pm2_5"] if cached else None,
                "us_aqi": cached["us_aqi"] if cached else None,
            }
        )
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
