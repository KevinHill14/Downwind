"""
Fire Tracker backend.

Serves:
- GET /                -> frontend (static/index.html)
- GET /api/fires       -> live fire detections from NASA FIRMS (VIIRS NRT), as GeoJSON-ish points
- GET /api/predict     -> heuristic spread prediction for a fire, using live wind data

NASA FIRMS requires a free MAP_KEY: https://firms.modaps.eosdis.nasa.gov/api/
Set it as the FIRMS_MAP_KEY environment variable before running.
"""
import math
import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

FIRMS_MAP_KEY = os.environ.get("FIRMS_MAP_KEY", "")
FIRMS_SOURCE = "VIIRS_SNPP_NRT"  # ~375m resolution, updated every ~3-4 hours
FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/1"

app = FastAPI(title="Fire Tracker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache: dict[str, dict] = {}  # area -> {"data": [...], "fetched_at": float}
CACHE_TTL_SECONDS = 15 * 60  # FIRMS refreshes every few hours; no need to hit it often


async def _fetch_firms_fires(area: str) -> list[dict]:
    if not FIRMS_MAP_KEY:
        raise HTTPException(
            status_code=500,
            detail="FIRMS_MAP_KEY environment variable is not set. "
            "Get a free key at https://firms.modaps.eosdis.nasa.gov/api/",
        )

    url = FIRMS_URL.format(key=FIRMS_MAP_KEY, source=FIRMS_SOURCE, area=area)
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


async def _get_area_fires(area: str) -> list[dict]:
    now = time.time()
    cached = _cache.get(area)
    if cached is not None and (now - cached["fetched_at"]) < CACHE_TTL_SECONDS:
        return cached["data"]
    fires = await _fetch_firms_fires(area)
    _cache[area] = {"data": fires, "fetched_at": now}
    return fires


@app.get("/api/fires")
async def get_fires(
    bbox: str | None = Query(
        None, description="west,south,east,north - restricts the query to a region (e.g. one country)"
    ),
    limit: int | None = Query(
        None, description="return only the N highest-intensity (FRP) fires, for a fast initial world view"
    ),
):
    area = "world"
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

    fires = await _get_area_fires(area)
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


def _destination_point(lat: float, lng: float, bearing_deg: float, distance_km: float) -> tuple[float, float]:
    """Move `distance_km` from (lat, lng) along `bearing_deg` (0=N, 90=E), great-circle."""
    R = 6371.0
    lat1 = math.radians(lat)
    lng1 = math.radians(lng)
    brng = math.radians(bearing_deg)
    d_r = distance_km / R

    lat2 = math.asin(
        math.sin(lat1) * math.cos(d_r) + math.cos(lat1) * math.sin(d_r) * math.cos(brng)
    )
    lng2 = lng1 + math.atan2(
        math.sin(brng) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lng2) + 540) % 360 - 180


@app.get("/api/predict")
async def predict_spread(
    lat: float = Query(...),
    lng: float = Query(...),
    frp: float = Query(10.0, description="Fire radiative power (MW), intensity proxy"),
    hours: float = Query(6.0, description="Hours to project forward"),
):
    """
    Heuristic (not physical) spread model, for demo purposes:
    - Live wind speed/direction fetched from Open-Meteo for the fire's location.
    - Base spread rate scales with wind speed and fire intensity (FRP).
    - Spread is elliptical: fastest downwind, slower crosswind/upwind
      (rough approximation of real fire behavior, e.g. Rothermel-style anisotropy).
    - Returns a polygon (list of lat/lng points) approximating the predicted perimeter.
    """
    wind_speed, wind_dir = await _fetch_wind(lat, lng)

    intensity_factor = min(2.0, 0.5 + math.log1p(frp) / 5)
    downwind_km = (0.15 * wind_speed + 0.3) * intensity_factor * hours / 6
    crosswind_km = downwind_km * 0.4
    upwind_km = downwind_km * 0.15

    perimeter = []
    n_points = 36
    for i in range(n_points):
        theta = 2 * math.pi * i / n_points  # 0 = downwind direction
        if math.cos(theta) >= 0:
            r = math.sqrt((downwind_km * math.cos(theta)) ** 2 + (crosswind_km * math.sin(theta)) ** 2)
        else:
            r = math.sqrt((upwind_km * math.cos(theta)) ** 2 + (crosswind_km * math.sin(theta)) ** 2)
        bearing = (wind_dir + math.degrees(theta)) % 360
        plat, plng = _destination_point(lat, lng, bearing, r)
        perimeter.append({"lat": plat, "lng": plng})

    return {
        "origin": {"lat": lat, "lng": lng},
        "hours": hours,
        "wind_speed_kmh": wind_speed,
        "wind_blowing_toward_deg": wind_dir,
        "perimeter": perimeter,
        "note": "Simplified heuristic model for demo purposes, not a physical fire behavior simulation.",
    }


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
