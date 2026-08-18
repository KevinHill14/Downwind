"""
Fire Tracker backend.

Serves:
- GET /                        -> frontend (static/index.html)
- GET /api/fires                -> live fire detections from NASA FIRMS (VIIRS NRT), as GeoJSON-ish points
- GET /api/wind                 -> live wind speed/direction for a location, used to point each fire's arrow
- POST /api/wind/batch          -> wind for many locations in a single Open-Meteo request
- POST /api/wind/forecast/batch -> hourly wind + humidity forecast for many locations, for the higher prediction-strength tiers
- POST /api/elevation/batch     -> ground elevation for many locations, used for the terrain/slope prediction tier

NASA FIRMS requires a free MAP_KEY: https://firms.modaps.eosdis.nasa.gov/api/
Set it as the FIRMS_MAP_KEY environment variable before running.
"""
import asyncio
import os
import time

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

_cache: dict[str, dict] = {}  # area -> {"data": [...], "fetched_at": float}
CACHE_TTL_SECONDS = 15 * 60  # FIRMS refreshes every few hours; no need to hit it often


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

    fires = []
    for line in lines[1:]:
        cols = line.split(",")
        try:
            fires.append(
                {
                    "lat": float(cols[idx["latitude"]]),
                    "lng": float(cols[idx["longitude"]]),
                    "brightness": float(cols[idx["bright_ti4"]]),
                    "confidence": cols[idx["confidence"]],
                    "frp": float(cols[idx["frp"]]),  # fire radiative power (MW), proxy for intensity
                    "acq_date": cols[idx["acq_date"]],
                    "acq_time": cols[idx["acq_time"]],
                }
            )
        except (ValueError, IndexError, KeyError):
            continue
    return fires


async def _get_area_fires(area: str, sources: list[str]) -> list[dict]:
    if not FIRMS_MAP_KEY:
        raise HTTPException(
            status_code=500,
            detail="FIRMS_MAP_KEY environment variable is not set. "
            "Get a free key at https://firms.modaps.eosdis.nasa.gov/api/",
        )

    # Cache key includes which sources were requested - a "fast" (one
    # source) world overview and the "full" (all sources) view of the same
    # area are genuinely different datasets, not interchangeable.
    cache_key = f"{area}|{','.join(sources)}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached is not None and (now - cached["fetched_at"]) < CACHE_TTL_SECONDS:
        return cached["data"]

    # All requested sources for this area in parallel - fetching them one at
    # a time would multiply the request latency by the number of satellites.
    # Each FIRMS source is itself a slow call for a whole-world area (NASA's
    # server takes seconds to generate the CSV, independent of our own
    # network), so for the fast initial "just show me the top N" world view
    # the caller passes only one source rather than all of them.
    results = await asyncio.gather(*(_fetch_firms_fires(area, source) for source in sources))
    fires = [f for source_fires in results for f in source_fires]
    _cache[cache_key] = {"data": fires, "fetched_at": now}
    return fires


WORLD_AREA = "-180,-90,180,90"


async def _warm_world_cache_loop():
    """NASA's server itself takes several seconds to generate a whole-world
    CSV, independent of anything on our end - refreshing the world caches
    proactively in the background (just before they'd expire) means a real
    visitor's first load almost always hits a warm cache instead of paying
    that cost live."""
    while True:
        try:
            await _get_area_fires(WORLD_AREA, FIRMS_SOURCES[:1])  # fast capped overview
            await _get_area_fires(WORLD_AREA, FIRMS_SOURCES)  # full "All fires" view
        except Exception:
            pass  # a transient NASA hiccup here shouldn't take down the warm loop
        await asyncio.sleep(max(60, CACHE_TTL_SECONDS - 120))


@app.on_event("startup")
async def _on_startup():
    if FIRMS_MAP_KEY:
        asyncio.create_task(_warm_world_cache_loop())


@app.get("/api/fires")
async def get_fires(
    bbox: str | None = Query(
        None, description="west,south,east,north - restricts the query to a region (e.g. one country)"
    ),
    limit: int | None = Query(
        None, description="return only the N highest-intensity (FRP) fires, for a fast initial world view"
    ),
):
    # FIRMS' API silently returns zero rows for the literal string "world" -
    # it needs an actual bbox, so the default (whole-earth) case uses the
    # full lat/lng range explicitly instead.
    area = "-180,-90,180,90"
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
        area = f"{west},{south},{east},{north}"

    # A bbox search (a specific country) or the explicit "All" world view
    # (limit=None) both want full completeness across every satellite. The
    # fast capped world overview (a limit, no bbox) only needs to find the
    # highest-FRP fires quickly, and one source's worth of candidates is
    # already far more than `limit` - not worth waiting on every satellite
    # just to throw most of the results away.
    sources = FIRMS_SOURCES if (bbox is not None or limit is None) else FIRMS_SOURCES[:1]
    fires = await _get_area_fires(area, sources)
    if limit is not None:
        fires = sorted(fires, key=lambda f: f["frp"], reverse=True)[:limit]

    return {"fires": fires, "count": len(fires)}


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
        except httpx.HTTPError:
            continue
        if isinstance(data, dict):  # Open-Meteo returns a plain object for a single point
            data = [data]
        for point, entry in zip(chunk, data):
            current = entry["current"]
            key = f"{_round_coord(point.lat)},{_round_coord(point.lng)}"
            _current_wind_cache[key] = {
                "wind_speed_kmh": current["wind_speed_10m"],
                "wind_blowing_toward_deg": (current["wind_direction_10m"] + 180) % 360,
                "fetched_at": now,
            }

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
        params = {
            "latitude": ",".join(str(p.lat) for p in uncached_points),
            "longitude": ",".join(str(p.lng) for p in uncached_points),
            "hourly": "wind_speed_10m,wind_direction_10m,relative_humidity_2m,precipitation,temperature_2m",
            "wind_speed_unit": "kmh",
            "forecast_days": forecast_days,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(WIND_URL, params=params)
            resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = [data]

        for point, entry in zip(uncached_points, data):
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
            key = f"{_round_coord(point.lat)},{_round_coord(point.lng)},{req.hours}"
            _forecast_cache[key] = {"steps": steps, "fetched_at": now}

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


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
