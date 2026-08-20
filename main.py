"""
Fire Tracker backend.

Serves:
- GET /                        -> frontend (static/index.html)
- GET /api/fires                -> live fire detections from NASA FIRMS (VIIRS NRT), as GeoJSON-ish points
- GET /api/wind                 -> live wind speed/direction for a location, used to point each fire's arrow
- POST /api/wind/batch          -> wind for many locations in a single Open-Meteo request
- POST /api/wind/forecast/batch -> hourly wind + humidity forecast for many locations, for the higher prediction-strength tiers
- POST /api/elevation/batch     -> ground elevation for many locations, used for the terrain/slope prediction tier
- GET /api/geocode               -> place name/address -> lat/lng, for the address risk-check feature

Fire data merges two sources into one in-memory dataset (see _world_fires):
NASA FIRMS (VIIRS satellite thermal detection, worldwide) and, Canada-only
for now, CWFIS's national ground-confirmed active fire list (see
_fetch_canada_fires) - satellite detection alone can miss a large fire
complex whose own smoke obscures its thermal signature, which
ground/dispatch reporting doesn't.

NASA FIRMS requires a free MAP_KEY: https://firms.modaps.eosdis.nasa.gov/api/
Set it as the FIRMS_MAP_KEY environment variable before running.
"""
import asyncio
import math
import os
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()  # picks up FIRMS_MAP_KEY from a local .env file, so it survives server restarts
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

FIRMS_MAP_KEY = os.environ.get("FIRMS_MAP_KEY", "")
# Querying every current VIIRS NRT satellite, not just SNPP - a single
# satellite only passes over a given spot a couple times a day, so relying
# on one source alone was silently missing a large share of active fires
# (confirmed against user reports of missing Ontario fires). All three are
# ~375m resolution and directly comparable, so results are just concatenated.
FIRMS_SOURCES = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]
# Day range of 2 (not 1) - NASA's near-real-time feed occasionally has a
# short ingestion gap where the most recent ~24h has nothing processed yet
# even though the source itself is fine (confirmed directly against FIRMS:
# day-range 1 returned zero rows worldwide while day-range 3 returned
# 179k), so this adds a small buffer against that rather than the app
# looking broken/keyless during a transient NASA-side gap.
FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/2"

app = FastAPI(title="Fire Tracker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

async def _fetch_firms_fires(area: str, source: str) -> list[dict]:
    url = FIRMS_URL.format(key=FIRMS_MAP_KEY, source=source, area=area)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    lines = resp.text.strip().splitlines()
    if not lines:
        return []

    header = lines[0].split(",")
    idx = {name: i for i, name in enumerate(header)}

    # brightness (bright_ti4) isn't kept - nothing on the frontend uses it
    # (only lat/lng/frp/confidence/acq_date/acq_time do), and skipping it
    # trims both the parse work and the JSON payload for large areas where
    # tens of thousands of rows are in play.
    fires = []
    for line in lines[1:]:
        cols = line.split(",")
        try:
            fires.append(
                {
                    "lat": float(cols[idx["latitude"]]),
                    "lng": float(cols[idx["longitude"]]),
                    "confidence": cols[idx["confidence"]],
                    "frp": float(cols[idx["frp"]]),  # fire radiative power (MW), proxy for intensity
                    "acq_date": cols[idx["acq_date"]],
                    "acq_time": cols[idx["acq_time"]],
                }
            )
        except (ValueError, IndexError, KeyError):
            continue
    return fires


CANADA_FIRES_URL = "https://cwfis.cfs.nrcan.gc.ca/downloads/reportedfires/activefires.csv"
# Stages that mean "currently burning" - EX (extinguished) is the only one
# that means it's out. BM ("being monitored") is included deliberately: a
# real, common fire management policy (used e.g. by Ontario) is to
# deliberately NOT suppress many remote fires that aren't threatening
# anything, watching them instead of fighting them - still a genuine active
# fire, just a different response posture than OC/UC/BH (out of control /
# under control / being held). Satellite hotspot detection has no way to
# represent that distinction; ground reporting does.
CANADA_ACTIVE_STAGES = {"OC", "UC", "BH", "BM"}


async def _fetch_canada_fires() -> list[dict]:
    """All of Canada's ground-confirmed active fires in one request - the
    Canadian Wildland Fire Information System's (CWFIS, run by Natural
    Resources Canada) national aggregation of every province/territory's own
    agency-reported fire data (CIFFC's coordinated national standard - the
    same field/stage conventions Ontario's own provincial service uses,
    just already merged across all of them here). No key required. Found by
    reverse-engineering a third-party fire tracker's network requests back
    to its real source, then confirming this was the single national feed
    behind it rather than 11+ separate provincial ones.

    This exists specifically to close a real gap satellite-only detection
    has: a large, well-established fire complex can generate enough smoke
    to obscure VIIRS/MODIS thermal detection over its own hottest core
    (confirmed directly - NASA's live feed had almost nothing for a region
    two other trackers showed a dense fire cluster in), but ground/dispatch
    reporting still knows the fire is there and how big it actually is.
    Canada-only for now, not deduplicated against satellite hotspots for the
    same fire (a large complex may show both a ground marker and nearby
    VIIRS pixels) - both are real signal, and matching them up would need
    real spatial-correlation work this first pass doesn't attempt."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(CANADA_FIRES_URL)
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
            # negative area - clamped to 0 here (rather than left negative)
            # since math.log1p below requires an input > -1, and this whole
            # fetch previously crashed silently on the very first sentinel
            # row it hit, dropping every ground fire for the entire refresh.
            hectares = max(0.0, float(cols[idx["fire_size"]] or 0))
            status_str = cols[idx["status_date"]] or cols[idx["situation_report_date"]]
            dt = datetime.fromisoformat(status_str) if status_str else datetime.now(timezone.utc)
            fires.append(
                {
                    "lat": lat,
                    "lng": lng,
                    # Ground-confirmed by agency dispatch, not a satellite
                    # guess - the highest confidence tier this app has.
                    "confidence": "h",
                    # No FRP is reported for ground-confirmed fires -
                    # hectares burned is the closest severity proxy,
                    # log-scaled so a 300,000ha complex doesn't render as
                    # literally thousands of times more "intense" than a
                    # 1ha one, and floored above LARGE_FIRE_MIN_FRP so
                    # every one of these renders as its own marker, never
                    # merged away - it's a real, verified incident, not an
                    # ambiguous single satellite pixel.
                    "frp": max(30.0, math.log1p(hectares) * 15),
                    "acq_date": dt.strftime("%Y-%m-%d"),
                    "acq_time": dt.strftime("%H%M"),
                    # Real hectares burned, kept alongside the derived FRP
                    # proxy above so ground fires can be filtered by their
                    # actual size (see min_hectares on /api/fires), not just
                    # the log-scaled severity number. Satellite detections
                    # have no equivalent field - absence of "ha" means
                    # "not a ground-sourced fire" everywhere this is read.
                    "ha": hectares,
                }
            )
        except (TypeError, ValueError, IndexError, KeyError):
            # One malformed row (unexpected sentinel, missing field) should
            # skip just that row, not silently drop every ground fire for
            # the whole refresh - this used to be the bug: the dict/frp
            # computation lived OUTSIDE this try block, so one bad row's
            # exception propagated all the way out of the function.
            continue
    return fires


WORLD_AREA = "-180,-90,180,90"
WORLD_REFRESH_INTERVAL_SECONDS = 10 * 60  # matches FIRMS' own NRT refresh cadence - polling faster wouldn't find anything new

# The entire world's fire data, held in memory and refreshed on a timer -
# NOT fetched per-request. This is the actual fix for "every new area you
# look at is slow": a per-area lazy cache (what this used to be) only ever
# speeds up a REPEAT visit to the same spot, since panning to anywhere new
# is a cache miss that pays NASA's ~5-10s whole-world CSV generation time
# live, every single time. A real fire tracker (FireMap.live etc.) almost
# certainly works this way too - a background worker ingests the feed on a
# schedule, and the site's own database (not NASA) answers every request.
# The whole world across 3 satellites is on the order of a few hundred
# thousand rows, small enough to hold in memory and filter/cluster in
# plain Python fast enough to matter - every /api/fires request becomes a
# local list comprehension, never a live upstream call.
_world_fires: list[dict] = []
_world_fires_fetched_at: float = 0.0


# ~0.15deg (roughly 15-17km at Canadian latitudes) - a VIIRS satellite
# pixel this close to a ground-reported fire is treated as a detection OF
# that same physical fire, not a separate one. Without this, a single large
# active fire renders as one ground marker PLUS a whole dense cluster of
# satellite pixels scattered across its own footprint, which is real signal
# but the same event shown many times over - a big fire visually reads as
# "way more fires than other trackers show" even though the underlying
# ground fire count is accurate. Doesn't need to be a precise great-circle
# distance for this purpose - it's a visual-dedup heuristic, not a safety
# calculation.
GROUND_DEDUP_RADIUS_DEG = 0.15


def _dedupe_against_ground(satellite_fires: list[dict], ground_fires: list[dict]) -> list[dict]:
    if not ground_fires:
        return satellite_fires

    # Bucket ground fires onto a grid sized to the dedup radius, so each
    # satellite fire only needs to check its own cell + 8 neighbors instead
    # of every ground fire - O(satellite + ground) instead of O(satellite *
    # ground), which matters since satellite counts run into the hundreds
    # of thousands.
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
    global _world_fires, _world_fires_fetched_at
    results = await asyncio.gather(*(_fetch_firms_fires(WORLD_AREA, source) for source in FIRMS_SOURCES))
    satellite_fires = [f for source_fires in results for f in source_fires]

    try:
        ground_fires = await _fetch_canada_fires()
    except Exception:
        # Canada's ground-source feed is a supplement, not the backbone -
        # if it's down or changes shape, satellite data (already fetched
        # above) should still go out rather than the whole refresh failing.
        ground_fires = []

    satellite_fires = _dedupe_against_ground(satellite_fires, ground_fires)
    _world_fires = satellite_fires + ground_fires
    _world_fires_fetched_at = time.time()


async def _world_fires_refresh_loop() -> None:
    while True:
        await asyncio.sleep(WORLD_REFRESH_INTERVAL_SECONDS)
        try:
            await _refresh_world_fires()
        except Exception:
            pass  # keep serving the last good data rather than let a transient NASA hiccup kill the loop


@app.on_event("startup")
async def _on_startup():
    if not FIRMS_MAP_KEY:
        return
    # Blocks server startup for one NASA-generation-time-sized wait (the
    # only time this cost is ever paid) so the very first real request is
    # already fast instead of racing an in-progress background fetch.
    await _refresh_world_fires()
    asyncio.create_task(_world_fires_refresh_loop())


def _filter_bbox(fires: list[dict], west: float, south: float, east: float, north: float) -> list[dict]:
    return [f for f in fires if west <= f["lng"] <= east and south <= f["lat"] <= north]


# Roughly the orange/red color boundary the frontend uses (see
# intensityT/FRP_SCALE_REF in static/index.html - t=2/3 on that log scale
# works out to ~27.4 MW, the top ~8-10% most intense fires worldwide). A
# fire at or above this renders as its own marker, never folded into a grid
# cell with weaker neighbors - a genuinely major fire can't get visually
# diluted or hidden by nearby small ones just because they share a cell.
# A lower cutoff (e.g. the orange/yellow boundary, ~4.3 MW) was tried first
# and rejected: that's below the MEDIAN fire's intensity, so it protected
# over 60% of all fires from clustering and took world-view load time from
# ~500ms back up to 7+ seconds, undoing earlier clustering performance work
# for basically no gain (most of what it "protected" wasn't actually a
# stand-out fire). Keep this in sync with the frontend's severity thresholds
# if FRP_SCALE_REF or the color bucket cutoffs change.
LARGE_FIRE_MIN_FRP = 27.4


def _cluster_fires(fires: list[dict], grid_deg: float) -> list[dict]:
    """Groups fires within grid_deg of each other into one FRP-weighted
    centroid marker - the same aggregation static/index.html's clusterFires()
    does client-side, but run here so a dense area's response is a few
    hundred/thousand aggregate points instead of tens of thousands of raw
    ones. Every fire still contributes fully to its cell's totals - nothing
    is dropped or deprioritized, unlike a top-N cap. Fires at or above
    LARGE_FIRE_MIN_FRP skip grouping entirely (see its docstring above)."""
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


@app.get("/api/fires")
async def get_fires(
    bbox: str | None = Query(
        None, description="west,south,east,north - restricts the query to a region (e.g. one country)"
    ),
    limit: int | None = Query(
        None, description="return only the N highest-intensity (FRP) fires, for a fast initial world view"
    ),
    grid: float | None = Query(
        None, description="pre-aggregate fires into grid_deg x grid_deg FRP-weighted clusters before returning"
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
        # A plain Python filter over the in-memory world set - no network
        # call, so this is the same cost whether it's the first time this
        # exact area has ever been requested or the thousandth.
        fires = _filter_bbox(fires, west, south, east, north)

    # Confidence-based filtering (unconditional "l" exclusion, plus a
    # stricter "h"-only floor when "Show small fires" is off) is TEMPORARILY
    # REVERTED for comparison - it dropped France from 300 detections to
    # just 1. "h" confidence turns out to be rare in VIIRS data generally
    # (real fires mostly land at "n" nominal, not "h"), so requiring it was
    # far too aggressive. min_confidence is accepted but currently ignored;
    # re-enable with a better-calibrated threshold once decided.

    if min_frp is not None:
        # A flat intensity floor, not a top-N cap - every fire at or above
        # min_frp still shows, regardless of how it ranks against fires
        # elsewhere. That distinction matters: a rank-based cap can bury a
        # real regional fire under stronger fires on the other side of the
        # world, which is exactly the failure mode this app avoids elsewhere.
        fires = [f for f in fires if f["frp"] >= min_frp]

    if min_hectares is not None:
        # Only affects ground-sourced fires (the only ones with an "ha"
        # field at all) - satellite detections pass through unfiltered by
        # this. Lets "significant fires only" be compared directly against
        # a fixed hectare threshold, same idea as min_frp but on the real
        # burned-area number instead of the derived FRP proxy.
        fires = [f for f in fires if f.get("ha") is None or f["ha"] >= min_hectares]

    raw_count = len(fires)

    if grid is not None:
        # Aggregated, not truncated - every one of raw_count fires still
        # contributes to a cluster below, just not as individual rows.
        fires = _cluster_fires(fires, grid)
    elif limit is not None:
        fires = sorted(fires, key=lambda f: f["frp"], reverse=True)[:limit]

    return {"fires": fires, "count": len(fires), "raw_count": raw_count}


WIND_URL = "https://api.open-meteo.com/v1/forecast"


async def _fetch_wind(lat: float, lng: float) -> tuple[float, float]:
    """Returns (wind_speed_kmh, blowing_toward_deg). Open-Meteo reports the
    direction wind is blowing FROM, so we flip it 180deg to get the direction
    fire would actually spread toward."""
    params = {
        "latitude": lat,
        "longitude": lng,
        "current": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kmh",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(WIND_URL, params=params)
        resp.raise_for_status()
    current = resp.json()["current"]
    speed = current["wind_speed_10m"]
    blowing_toward = (current["wind_direction_10m"] + 180) % 360
    return speed, blowing_toward


@app.get("/api/wind")
async def get_wind(lat: float = Query(...), lng: float = Query(...)):
    """Live wind for a location - used to point each fire's direction arrow."""
    speed, wind_dir = await _fetch_wind(lat, lng)
    return {"wind_speed_kmh": speed, "wind_blowing_toward_deg": wind_dir}


class WindPoint(BaseModel):
    lat: float
    lng: float


@app.post("/api/wind/batch")
async def get_wind_batch(points: list[WindPoint]):
    """Wind for many locations at once, e.g. one per predicted fire cluster
    or one per cell of the worldwide wind overlay grid. Open-Meteo supports
    comma-separated lat/lng lists natively, so this is a handful of outbound
    requests (chunked - see WIND_BATCH_CHUNK_SIZE) rather than one per point,
    which was the main bottleneck when predicting for many markers at once.
    Also cached per-point like the forecast/elevation endpoints, since the
    wind overlay re-requests the same worldwide grid every time it's toggled."""
    if not points:
        return {"results": []}

    now = time.time()
    uncached_points = []
    for p in points:
        key = f"{_round_coord(p.lat)},{_round_coord(p.lng)}"
        cached = _current_wind_cache.get(key)
        if cached is None or (now - cached["fetched_at"]) >= FORECAST_CACHE_TTL_SECONDS:
            uncached_points.append(p)

    # Open-Meteo is a GET API - a long enough comma-separated point list hits
    # the URL length limit (414), so uncached points go out in bounded chunks.
    # One chunk hitting a transient error (e.g. Open-Meteo's rate limit)
    # shouldn't blank out the whole overlay - that chunk's points are just
    # skipped rather than failing the entire request.
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
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(WIND_URL, params=params)
                resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            # Was silently swallowed with no trace of what actually failed -
            # a 200 OK in the access log told us nothing about whether this
            # outbound call to Open-Meteo even succeeded. Printed so it shows
            # up in the host's logs (e.g. Render) when the wind overlay/
            # predictions come back empty in production.
            print(f"[wind/batch] Open-Meteo request failed: {type(e).__name__}: {e}")
            continue
        if isinstance(data, dict):  # Open-Meteo returns a plain object for a single point
            data = [data]
        for point, entry in zip(chunk, data):
            # One malformed entry (Open-Meteo occasionally returns an error
            # object for a single bad point within an otherwise-good batch)
            # used to throw here uncaught, 500ing the whole request and
            # losing every other point in the batch along with it. Skipping
            # just that point keeps the rest of the batch - and the request -
            # alive.
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
    points: list[WindPoint]
    hours: int


# Per-point caches, keyed by rounded coordinates (~110m precision - plenty
# for wind/terrain, which don't vary meaningfully at that scale). Re-running
# a prediction (switching prediction strength, nudging the day slider, a
# zoom-triggered re-cluster) re-requests mostly-the-same cells, and Open-
# Meteo's free tier rate-limits fairly aggressively - caching avoids hitting
# that on normal repeated use, not just on first load like the FIRMS cache.
_forecast_cache: dict[str, dict] = {}  # "lat,lng,hours" -> {"steps": [...], "fetched_at": float}
_elevation_cache: dict[str, dict] = {}  # "lat,lng" -> {"elevation_m": float, "fetched_at": float}
_current_wind_cache: dict[str, dict] = {}  # "lat,lng" -> {"wind_speed_kmh", "wind_blowing_toward_deg", "fetched_at"}
FORECAST_CACHE_TTL_SECONDS = 15 * 60  # matches the FIRMS cache TTL
ELEVATION_CACHE_TTL_SECONDS = 24 * 60 * 60  # terrain doesn't change; cached long, not forever, in case of bad reads


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
        # (e.g. France) - up to 168 hours x 5 variables x every point in one
        # go - was genuinely slow enough on Open-Meteo's end to blow past a
        # flat 15s client timeout, silently returning zero predictions for
        # that entire country while a small country (few points) finished in
        # time. Chunking (same pattern as /api/wind/batch) keeps each
        # individual request small/fast and lets one slow or failed chunk
        # skip just its own points instead of taking out the whole country;
        # chunks run concurrently so this isn't slower overall than the one
        # big request was meant to be.
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
                async with httpx.AsyncClient(timeout=25) as client:
                    resp = await client.get(WIND_URL, params=params)
                    resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as e:
                print(f"[wind/forecast/batch] Open-Meteo request failed for a "
                      f"{len(chunk)}-point chunk: {type(e).__name__}: {e}")
                return
            if isinstance(data, dict):
                # Open-Meteo returns a single object (not a list) both for a
                # genuine one-point request AND, apparently, for some error
                # conditions (e.g. exceeding its per-request location
                # limit) - in the error case this single dict is NOT a
                # per-point forecast, and wrapping it in a 1-element list
                # would silently zip it against only the FIRST point while
                # every other point in the chunk gets nothing, no
                # exception, no log. A real forecast object always has an
                # "hourly" key; anything else is almost certainly an error
                # payload, worth seeing verbatim.
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
                # A single bad point shouldn't take out the rest of the
                # chunk - skip just this one and keep going.
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
async def get_elevation_batch(points: list[WindPoint]):
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
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(ELEVATION_URL, params=params)
            resp.raise_for_status()
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
# Nominatim's usage policy requires an identifying User-Agent and caps at
# ~1 request/sec for this kind of light, non-bulk use - fine here since it's
# only a fallback, not the primary path.
NOMINATIM_HEADERS = {"User-Agent": "hacksocial26-fire-tracker/1.0 (hackathon project)"}


@app.get("/api/geocode")
async def geocode(q: str = Query(..., min_length=1, description="free-text place name or address to look up")):
    """Turns a place name/address into a lat/lng, for the 'is my home in
    danger' address lookup. Tries Open-Meteo first (same vendor as the
    wind/elevation endpoints - free, no key, no documented rate limit), but
    it's a place-name gazetteer - cities, towns, landmarks - and often
    misses a full street address ('123 Main St, Springfield'). Nominatim
    (OpenStreetMap) covers those, so it's a second attempt when Open-Meteo
    comes up empty, not the primary path (its stricter rate limit makes it
    a worse default for every lookup)."""
    params = {"name": q, "count": 1, "language": "en", "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(GEOCODE_URL, params=params)
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
        async with httpx.AsyncClient(timeout=10, headers=NOMINATIM_HEADERS) as client:
            resp = await client.get(
                NOMINATIM_URL,
                params={"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            )
            resp.raise_for_status()
        nominatim_results = resp.json()
    except httpx.HTTPError:
        nominatim_results = []

    if not nominatim_results:
        raise HTTPException(status_code=404, detail=f"Couldn't find a location matching '{q}'")

    top = nominatim_results[0]
    # display_name is Nominatim's full address string (house number through
    # postcode, 8-10 comma-separated parts for a precise street address) -
    # useful for disambiguation, unusable as a UI label. jsonv2's structured
    # `address` object lets us build a short "street, city" style name
    # instead, the same shape Open-Meteo already returns for its results.
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
