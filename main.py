"""
Fire Tracker backend.

Serves:
- GET /                -> frontend (static/index.html)
- GET /api/fires       -> live fire detections from NASA FIRMS (VIIRS NRT), as GeoJSON-ish points

NASA FIRMS requires a free MAP_KEY: https://firms.modaps.eosdis.nasa.gov/api/
Set it as the FIRMS_MAP_KEY environment variable before running.
"""
import os
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

FIRMS_MAP_KEY = os.environ.get("FIRMS_MAP_KEY", "")
FIRMS_SOURCE = "VIIRS_SNPP_NRT"  # ~375m resolution, updated every ~3-4 hours
FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/world/1"

app = FastAPI(title="Fire Tracker")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache = {"data": None, "fetched_at": 0}
CACHE_TTL_SECONDS = 15 * 60  # FIRMS refreshes every few hours; no need to hit it often


async def _fetch_firms_fires() -> list[dict]:
    if not FIRMS_MAP_KEY:
        raise HTTPException(
            status_code=500,
            detail="FIRMS_MAP_KEY environment variable is not set. "
            "Get a free key at https://firms.modaps.eosdis.nasa.gov/api/",
        )

    url = FIRMS_URL.format(key=FIRMS_MAP_KEY, source=FIRMS_SOURCE)
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


@app.get("/api/fires")
async def get_fires():
    now = time.time()
    if _cache["data"] is not None and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
        return {"fires": _cache["data"], "cached": True, "fetched_at": _cache["fetched_at"]}

    fires = await _fetch_firms_fires()
    _cache["data"] = fires
    _cache["fetched_at"] = now
    return {"fires": fires, "cached": False, "fetched_at": now}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
