# Fire Tracker

![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688.svg)
![Frontend](https://img.shields.io/badge/frontend-Three.js%20%2F%20globe.gl-black.svg)
![Status](https://img.shields.io/badge/status-hackathon%20project-orange.svg)
![License](https://img.shields.io/badge/license-none-lightgrey.svg)

A 3D rotating globe that shows real, currently-burning wildfires worldwide, and can project where a fire is likely to spread based on wind, terrain, and weather.

Fire data comes straight from satellites (NASA FIRMS) and, for Canada, ground-confirmed agency reports (CWFIS) — not a static dataset. The globe is a single-page Three.js/`globe.gl` frontend; the backend is a small FastAPI server that aggregates fire data and proxies weather/geocoding lookups.

---

## Table of contents

- [Features](#features)
- [Architecture](#architecture)
- [Running it locally](#running-it-locally)
- [Configuration](#configuration)
- [API reference](#api-reference)
- [What we learned](#what-we-learned)
- [Problems we ran into (and how we handled them)](#problems-we-ran-into-and-how-we-handled-them)
- [Known limitations / ideas not built yet](#known-limitations--ideas-not-built-yet)

---

## Features

### Live fire map
- Every active fire detection worldwide, refreshed every 10 minutes from 3 NASA VIIRS satellites (SNPP, NOAA-20, NOAA-21) plus Canada's ground-confirmed active fire list.
- Color/severity tiers by Fire Radiative Power (FRP — a proxy for intensity): yellow → orange → red → **extreme** (≥150 MW) → **catastrophic** (≥450 MW), log-scaled so the color range stays meaningful across both a small brush fire and a megafire.
- **Detail level** slider (0–6): controls how aggressively nearby fires are grouped into one FRP-weighted marker. Nothing is ever dropped or capped by rank — every fire still contributes to a cluster, the slider only changes how coarse the grouping is. Fires above a set intensity always render as their own marker regardless of the slider, so a major fire can never get visually diluted into a cluster.
- Ground-confirmed (Canada) fires are de-duplicated against satellite hits for the same physical fire, so a large fire complex doesn't show up as one ground marker *plus* a dense cloud of satellite pixels on top of it.

### Fire spread prediction
Select a country (via search) to see predicted spread for its active fires, at 4 increasing levels of accuracy:

| Strength | Name | What it adds |
|---|---|---|
| 1 | Basic | One current wind reading → straight-line projected spread. |
| 2 | Hourly wind *(default)* | Wind refetched every 6 hours over the prediction window, so the path visibly bends as wind shifts. |
| 3 | +Terrain | Adds one elevation probe per fire — spread is biased faster downhill / slower uphill. |
| 4 | Max accuracy | Terrain recomputed every step against that step's actual wind direction, plus humidity, temperature, and precipitation factored into spread speed. |

Prediction window is adjustable 1–7 days. This is explicitly a demo-grade approximation, not a physical fire-behavior simulation — good for visualizing *a* plausible spread direction, not for real operational decisions.

**Monte Carlo mode** (toggle, off by default — only available while Prediction is on): a single projected line quietly implies the forecast wind is exact. With this on, the same spread math is re-run 32 times per fire with the wind perturbed within its realistic forecast error, and the region those scenarios land in is outlined in cyan underneath the main projection — so you can see *how confident the projection actually is*. A tight outline means the wind is consistent and the projection is trustworthy; a wide one means it genuinely could go several ways. Costs no extra API calls (it re-uses the wind data already fetched), and since every fire's footprint lives in one merged mesh, every marker gets one.

**Playback** — a "See animation" button walks every fire from where it is now to its projected position over the prediction window, so the spread reads as motion rather than a static line. The button doubles as the clock (`Stop (+28h)`). Only the markers move; the projected paths stay drawn underneath as the route, which keeps it to a single buffer update per frame instead of rebuilding hundreds of lines.

### "Am I in danger?" address check
Type an address, and the app geocodes it, looks at nearby fires (including their predicted spread), and returns a **Safe / Watch / Danger** badge with the nearest real threat's distance, confidence, and either how long ago it was detected or its predicted time of arrival.

With **Monte Carlo** on, it also reports how often a fire actually reaches you across sampled wind scenarios — *"100% of 18 wind scenarios bring a fire within watch range, 6% within danger range"* — instead of a single yes/no. If a substantial share of scenarios come out worse than the single best-guess wind, the rating is raised to match and says so: a 1-in-3 chance of being in danger shouldn't display as a flat "SAFE" just because the most likely wind happens to miss.

### Save / share the current view
A "Save image" button captures the globe exactly as it looks and adds a caption bar with the live fire count, timestamp, and data credits. On mobile it hands the image to the native share sheet — which is where "Save Image" (camera roll) lives, alongside messaging apps; on desktop it downloads.

### Click a fire for details
Click any marker for its exact position, detection count, total and peak FRP, area burned (ground-sourced fires), confidence, and how recently it was detected — or, for a projected marker, its lead time and the wind driving it. Implemented by raycasting the existing batched marker cloud **on click only**; hover would mean hit-testing on every mouse move, which is what made an earlier attempt at this laggy.

### Country search & quick navigation
Type-ahead country search flies the camera to the country and loads only fires inside its actual border (point-in-polygon filtered, not just a bounding box). Continent quick-look buttons jump the camera to a preset view.

### Filters
- **Show small fires** (off by default) — otherwise low-intensity/low-confidence detections are hidden to cut down on visual noise for a world-scale view.
- **Show minor ground fires** (on by default) — Canada's ground-confirmed fires can be filtered by hectares burned, independent of the satellite FRP filter above.

### Mobile support
Responsive layout below 820px width: search and settings collapse into icon-triggered overlay panels, the prediction/address panels share one screen slot instead of competing for space, and sliders/touch targets resize for a touch screen.

---

## Architecture

```
┌─────────────────────────────┐         ┌───────────────────────────────┐
│   static/index.html          │         │            main.py             │
│   (single-file frontend)     │  HTTP   │         (FastAPI backend)      │
│                               │────────▶│                                 │
│  - Three.js + globe.gl        │◀────────│  /api/fires                    │
│  - all UI, all prediction     │  JSON   │  /api/wind/batch                │
│    math, all rendering        │         │  /api/wind/forecast/batch      │
└──────────────────────────────┘         │  /api/elevation/batch          │
                                          │  /api/geocode                  │
                                          └─────────────┬───────────────────┘
                                                         │
                                     ┌───────────────────┼──────────────────────┐
                                     ▼                    ▼                      ▼
                            NASA FIRMS (VIIRS)     CWFIS (Canada)         Open-Meteo /
                            satellite fire data    ground-confirmed        Nominatim
                            (needs a free key)      fires (no key)     (wind/elevation/geocoding,
                                                                            no key needed)
```

The backend holds the *entire world's* fire data in memory, refreshed on a timer — not fetched per-request — so a browser panning anywhere on the globe is just a fast in-memory filter, never a live round-trip to NASA. Weather/terrain lookups for predictions are proxied through the backend (not called from the browser directly) so they can be batched, cached, and rate-limited server-side.

---

## Running it locally

### Prerequisites
- Python 3.11+
- A free NASA FIRMS API key: **https://firms.modaps.eosdis.nasa.gov/api/** (takes a couple minutes, no approval wait)

### Setup

```bash
git clone <this repo>
cd hacksocial26

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
```

Create a `.env` file in the project root:

```
FIRMS_MAP_KEY=your_key_here
```

### Run

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000** in a browser.

The server becomes ready almost immediately; real fire data populates in the background a few seconds later (the map will briefly show 0 fires while NASA's response comes in — this is intentional, see [Problems](#problems-we-ran-into-and-how-we-handled-them) below for why).

> Without `FIRMS_MAP_KEY` set, the server still starts and serves the frontend, but `/api/fires` returns a clear 500 error explaining the missing key instead of silently showing an empty map.

---

## Configuration

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `FIRMS_MAP_KEY` | Yes | Free NASA FIRMS API key. Without it, fire data can't be fetched at all. |

### Backend tuning constants (`main.py`)

These aren't environment variables — they're constants in the code, listed here for anyone tuning behavior rather than reading the whole file.

| Constant | Value | Controls |
|---|---|---|
| `WORLD_REFRESH_INTERVAL_SECONDS` | 10 min | How often the whole world's fire data is re-fetched from NASA/CWFIS. |
| `FORECAST_CACHE_TTL_SECONDS` | 15 min | How long a wind/forecast reading for a given point is reused before re-fetching. |
| `ELEVATION_CACHE_TTL_SECONDS` | 24 hr | How long a terrain reading is cached (terrain doesn't change, so this is long). |
| `CACHE_EVICTION_INTERVAL_SECONDS` | 2 min | How often expired cache entries are actually swept out of memory. |
| `MAX_BATCH_POINTS` | 1000 | Hard cap on points accepted in one wind/elevation/forecast request. |
| `_OPEN_METEO_CONCURRENCY` | 1 | How many outbound weather API calls can be in flight at once, across every user. Deliberately fully serialized — see [Problems](#problems-we-ran-into-and-how-we-handled-them). |
| `_FIRES_COMPUTE_SEMAPHORE` | 20 | How many `/api/fires` requests can be doing real filter/cluster work at once before new ones get a fast "busy, retry" response instead of queuing indefinitely. |
| `LARGE_FIRE_MIN_FRP` | 27.4 MW | Fires at or above this intensity always render as their own marker, never grouped into a cluster. |
| `GROUND_DEDUP_RADIUS_DEG` | 0.15° (~15-17km) | How close a satellite detection has to be to a ground-confirmed fire to be treated as "the same fire" and hidden. |

### Frontend tuning constants (`static/index.html`)

| Constant | Value | Controls |
|---|---|---|
| `DETAIL_GRID_STEPS` | `[3.0 … 0.1]` | The 7 clustering-grid sizes (degrees) the Detail level slider steps through. |
| `FRP_SCALE_REF` | 150 MW | The FRP value color intensity is normalized against. |
| `CATASTROPHIC_FRP_THRESHOLD` | 450 MW (3× ref) | Threshold for the darkest/most severe marker color. |
| `COUNTRY_LABEL_CAP` | 45 | Max country/water-body name labels rendered at once. |
| `LABEL_VISIBILITY_FACTOR` | 6 | A label appears once camera altitude is within 6× that feature's "natural framing" altitude. |
| `WIND_GRID_DEG` | 0.3° (~30km) | Nearby fire clusters within this distance share one wind lookup instead of querying per-fire. |
| `MONTE_CARLO_SAMPLES` | 18 | Wind scenarios simulated per fire in Monte Carlo mode. |
| `MONTE_CARLO_PATH_BUDGET` | 700 | Max sample paths rendered at once; caps how many markers get a fan (budget ÷ samples). |
| `MC_BEARING_SD_DEG` | 22° | Assumed scenario-level wind direction uncertainty. |
| `MC_SPEED_SD_FRAC` | 0.28 | Assumed wind speed uncertainty (±~28%). |
| `MC_FOOTPRINT_ALTITUDE` | 0.006 | Height of the uncertainty footprint above the globe (under the projected path). |
| `MC_RISK_ESCALATION_FRACTION` | 0.30 | Share of scenarios that must reach a risk level before the address check raises its rating to match. |
| `PREDICTION_ANIMATION_DURATION_MS` | 7000 | How long the "See animation" playback takes to cover the whole prediction window. |
| `FIRE_CLICK_TOLERANCE_PX` | 12 | Click tolerance for selecting a fire marker, in screen pixels (converted to world units per zoom level). |

---

## API reference

All endpoints are JSON. None require auth from the browser (the backend holds the only key needed, for FIRMS).

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/fires` | GET | Fire data. `bbox`, `grid`, `min_frp`, `min_confidence`, `min_hectares` query params filter/cluster it. |
| `/api/wind/batch` | POST | Current wind for a list of `{lat, lng}` points. |
| `/api/wind/forecast/batch` | POST | Hourly wind + humidity + precipitation + temperature forecast for a list of points, over `hours` hours. |
| `/api/elevation/batch` | POST | Ground elevation for a list of points. |
| `/api/geocode` | GET | Place name / address (`q`) → `{lat, lng, name, ...}`. |

---

## What we learned

- **Batching API calls matters more than almost anything else.** Every weather/terrain lookup in this app is batched (many points per HTTP request) instead of one-call-per-point — the difference between a prediction taking 1 second and taking 40 is almost entirely this. Free third-party APIs are also often rate-limited *by request count*, not by data volume, so fewer, bigger calls is close to free performance.
- **A rate limit isn't always about your own traffic.** We assumed persistent `429`s from our weather provider meant *our app* was calling too often. It turned out our hosting platform's free tier shares outbound IP addresses across unrelated customers, and the provider rate-limits by IP — so the limit could be getting hit by traffic that has nothing to do with us. No amount of tuning our own request pacing can fix a quota someone else is using up. The real fix was accepting that external dependency can't be made 100% reliable, and building a fallback so the app still works when it isn't.
- **Retrying isn't always the right move.** Our first instinct when a request failed was "retry with backoff." That's correct for a brief burst, but actively makes things worse against a *sustained* outage — every retry is more load on an already-overwhelmed resource, and it just delays failure instead of preventing it. The fix was learning to tell the two situations apart from real logs, not assumptions.
- **A synchronous loop over tens of thousands of items blocks *everything* on a single-threaded server**, not just the request that triggered it — including completely unrelated, otherwise-instant requests. This is easy to miss locally (fast machine, low data volume) and only shows up under real concurrent load.
- **Caching the fully-rendered output, not just the raw data, matters.** Caching a Python object still leaves you re-serializing and re-compressing it on every request; caching the actual response bytes (including the pre-gzipped variant) is what actually removes that repeated cost.
- **Geospatial deduplication and clustering are both just "put things in grid cells" in disguise** — nearest-neighbor-ish problems that look like they need something fancy usually don't; rounding a coordinate to a grid cell key gets you 90% of the way there for a fraction of the complexity of a real spatial index.

---

## Problems we ran into (and how we handled them)

### Server & client performance
Two separate performance problems, at two different layers:

- **Server-side**: fetching and holding every fire worldwide in memory (as plain Python dicts) got uncomfortably close to the free hosting tier's memory ceiling before a single request had even come in. Switched every fire to a memory-compact `@dataclass(slots=True)` object, which dropped baseline memory by roughly half. Separately, clustering thousands of fires is real CPU work with no natural `await` point — running it directly in the request handler was found (via load testing) to freeze the *entire server* for every other concurrent user, not just the request doing the clustering. Moving that work onto a background thread fixed it without changing the clustering itself.
- **Client-side**: rendering thousands of individual fire markers as separate 3D objects would be far too slow to animate smoothly. All fire markers are one batched point-sprite draw call instead. Country/ocean name labels turned out to be the single most expensive thing to rebuild every frame — they're now hidden entirely during camera motion and only rebuilt once the camera settles, which was the single biggest fix for drag/zoom smoothness. Capping the rendering pixel ratio on high-DPI screens (where GPU cost scales with pixel count, not visible sharpness) was another large, low-effort win.

### Getting accurate data from NASA
NASA's near-real-time fire feed has real gaps: any single satellite pass only covers a spot a couple times a day, and the feed occasionally has a short ingestion delay for the most recent hours. Using all 3 active VIIRS satellites (not just one) and pulling a 2-day window (not 1) closes most of that gap. Satellite thermal detection also has a real blind spot: a large, well-established fire can generate enough of its own smoke to obscure its hottest core from above — so ground-confirmed reports (Canada, via CWFIS) are merged in as a second, independent source, deduplicated against satellite hits for the same physical fire so it doesn't double-count.

### Creating names and markers without causing lag
Both fire markers and place-name labels have the same underlying risk: naively rendering "one of everything currently on screen" scales badly once there are tens of thousands of fires or 190+ countries all visible at once.
- **Markers**: server-side clustering means a dense region sends back a few hundred aggregate points instead of tens of thousands of raw ones — nothing is dropped, every fire still contributes to a cluster's position/intensity, but the browser never has to render more than a manageable number of points.
- **Labels**: capped at 45 visible at once (largest/most significant first), only shown once the camera is close enough to that specific feature to matter, and entirely skipped during camera motion — since building label geometry turned out to be the most expensive per-frame operation in the whole app, and it's not something the user can actually read while the camera is still moving anyway.

### Predictions (this took the most iteration by far)
The prediction feature depends on a free third-party weather API, and getting it to work *reliably* was the single hardest problem in this project — it went through several rounds of fixes that each addressed a real but incomplete piece of the picture:

1. **First problem**: a burst of simultaneous requests (multiple fire-spread chunks, or multiple people predicting at once) would trip the weather API's rate limit outright.
   → Added a concurrency cap so requests queue instead of all firing at once.
2. **Second problem**: when a request *did* get rate-limited, several chunks would retry with the exact same backoff delay and collide again on every retry attempt — the smarter backoff math never actually mattered because the requests stayed in lockstep.
   → Added randomized jitter so concurrent retries spread out instead of re-colliding.
3. **Third problem**: even with the above fixed, predictions for larger countries kept failing. Real production logs showed retries backing off for well over a minute and *still* getting rate-limited — proof this wasn't a brief burst that patience would fix, it was sustained.
   → Investigated the actual cause: the hosting platform's free tier shares outbound IP addresses across unrelated customers, and the weather API rate-limits by IP. The limit being hit likely had little to do with our own traffic. Fully serialized all outbound weather requests to one at a time (the gentlest possible request rate) and shortened the retry budget, since retrying into a sustained external outage just delays failure rather than preventing it.
4. **Fourth problem**: even serialized, an external, shared-infrastructure rate limit can't be guaranteed to *never* happen — and when it did, the prediction would come back with visible gaps or fail entirely for that area.
   → Added a two-level fallback: a point whose weather data couldn't be fetched now borrows the nearest point's real reading that *did* succeed; if literally nothing in the whole request succeeded, it falls back to a plausible synthetic default. A prediction now always returns a marker for every point, regardless of the weather API's availability — it just occasionally trades exact precision for an approximation instead of failing visibly.

The broader lesson from this whole chain: the first few fixes were all real improvements, but each was solving one layer of a problem that had another layer underneath it, discoverable only by testing against real production logs rather than local conditions (a personal, non-shared IP never reproduced the underlying issue at all).

---

## Validating the prediction math

`validate_predictions.py` measures the spread projection against what actually happened: it takes fires as they were on a past day, re-runs the same projection using the wind that really blew, and compares against detections that followed.

```bash
python validate_predictions.py --bbox=-10,36,4,44 --days-ago 4 --hours 24
```

It reports error against a **persistence baseline** ("predict the fire doesn't move"), because fire detections cluster — any prediction lands near *some* fire, so absolute error alone means nothing. It also sweeps the spread-rate constant and tests whether wind direction carries signal.

**What it found, honestly:** the projection does *not* beat persistence, and error grows the further it projects. That looks damning, but the ground truth turns out to be biased — smoke blows downwind and obscures satellite thermal detection there, so detections are systematically missing exactly where spread is predicted. Measured directly, new detections skew *upwind* (36–61° from reported wind, versus 90° for random). That's a detection artifact, not fire behaviour: Open-Meteo uses the meteorological convention, and the app's handling of it is correct.

The honest conclusion is that point detections can't cleanly validate a spread model — that needs mapped fire perimeters. The script stands as a regression check on the math and a sanity check on magnitudes, and it is deliberately written to argue against its own headline number rather than quote it.

## Known limitations / ideas not built yet

- Prediction is a **demo-grade approximation** of fire spread, not a physical simulation — good for visualizing plausible direction/reach, not for real operational/safety decisions.
- Ground-confirmed (agency-reported) fire data currently covers Canada only.
- Ocean name labels reuse the country label system but aren't fully built out.
- A compass showing camera orientation relative to the rest of the world, and a "jump to my own location" street-view-style button, were discussed but not built.
