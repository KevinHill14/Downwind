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

### Smoke forecast (circle a region)
Fire proximity is only half of the danger. The overwhelming majority of people a wildfire harms never see flame — they breathe the smoke, which travels hundreds of kilometres downwind.

Click **Smoke check**, then click anywhere on the globe and pick a radius. The app finds every fire inside that circle, groups them into smoke sources, pulls the **measured** PM2.5 at the strongest ones, and projects where that smoke goes over the next 24 hours using the forecast wind — drawn as a plume whose colour *and* opacity are set per-vertex from the US AQI at that distance. So it reads as dark and solid where the air is genuinely hazardous and fades to nothing where the smoke has diluted back to background. A flat translucent shape would have shown where smoke travels while saying nothing about how bad it is when it gets there.

The readout gives the measured AQI at the fire, the direction and reach of the plume, modelled AQI at 50/150/300 km, and which countries it passes over. Concentration downwind is anchored on a real measurement at the source rather than an emission estimate, then decayed for plume widening and deposition. Smoke is advected at ~90% of wind speed — unlike a fire *front*, which is limited by what it can burn through, an airborne particle isn't.

### Air quality in the address check
The address check now reports ground-level air quality alongside the fire verdict — never folded into it, because they're genuinely different risks with different responses. Without this, somebody 200 km downwind of a megafire got a confident **SAFE** while breathing hazardous air.

Unlike the wind endpoints, this has **no synthetic fallback**: a made-up wind direction only misaims a drawing on a globe, but a made-up air quality figure is a health number someone might act on. If the measurement isn't available, the line is omitted entirely rather than guessed at.

### Last 7 days (playback & scrubber)
The live map answers *"what is burning now"*. This answers *"which way has it been going"* — which a single snapshot structurally cannot. One day of detections is a scatter of dots; the same region played day by day shows a fire front actually moving across the ground. It's also the only part of the app that is pure observation rather than forecast.

Press **Last 7 days** to fetch the week for whatever region is on screen, then play it, pause it, or drag the scrubber to any single day. Leaving playback restores the live view exactly.

### Biggest fires right now
The globe opens on tens of thousands of markers with nowhere obvious to start, and every other feature here only becomes reachable once you've picked somewhere to look. This panel is that entry point: the five places on Earth burning hardest, one click from view.

Ranked on its own fixed clustering grid rather than the current display settings, so an unrelated Detail level change can't alter what it ranks. Clusters within 250 km merge and each country is capped at two rows — without that, a bad week in Siberia filled all five rows with the word "Russia".

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

### Loading behaviour
The globe is handed over as soon as it exists rather than being held behind a loading screen. The map can be panned and zoomed immediately; only the controls that genuinely need world fire data (search, smoke check, history, the address check) dim until it lands, so a slow fetch reads as *"this part isn't ready yet"* rather than *"the site is broken"*.

The two responses a first-time visitor always requests are pre-built on the data refresh timer, so nobody pays to construct them. Cold load of the default view went from **29.4s to 0.39s** — see [Problems](#problems-we-ran-into-and-how-we-handled-them).

If the server is still doing its own first fetch from NASA (a fresh deploy, or free-tier instance waking up), `/api/fires` says so explicitly and the page waits it out — rather than drawing an empty globe that looks exactly like a world with no fires on it.

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
│  - all UI, all prediction     │  JSON   │  /api/fires/history             │
│    math, all rendering        │         │  /api/wind/batch                │
└──────────────────────────────┘         │  /api/wind/forecast/batch      │
                                          │  /api/elevation/batch          │
                                          │  /api/air-quality/batch        │
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
| `_WORLD_VIEW_CACHE_MAX_ENTRIES` | 8 | Max cached world-view responses (LRU). Bounds cache memory — an unbounded version of this cache caused an out-of-memory restart in production. |
| `_DEFAULT_VIEW_GRID` / `_DEFAULT_VIEW_MIN_FRP` / `_ESTIMATE_GRID` | 0.5 / 3.0 / 0.05 | The exact request a first-time visitor makes. These two responses are pre-built on every data refresh so nobody pays to construct them — keep in sync with the frontend defaults. |
| `_OPEN_METEO_CONCURRENCY` | 1 | How many outbound weather API calls can be in flight at once, across every user. Deliberately fully serialized — see [Problems](#problems-we-ran-into-and-how-we-handled-them). |
| `_FIRES_COMPUTE_SEMAPHORE` | 20 | How many `/api/fires` requests can be doing real filter/cluster work at once before new ones get a fast "busy, retry" response instead of queuing indefinitely. |
| `LARGE_FIRE_MIN_FRP` | 27.4 MW | Fires at or above this intensity always render as their own marker, never grouped into a cluster. The history endpoint deliberately overrides this to cluster everything — see [Problems](#problems-we-ran-into-and-how-we-handled-them). |
| `AIR_QUALITY_CACHE_TTL_SECONDS` | 30 min | How long a PM2.5/AQI reading is reused. Longer than the wind TTL because air quality is only published hourly. |
| `HISTORY_MAX_DAYS` | 7 | Longest history window. FIRMS caps a single request at 5 days, so a 7-day window is fetched as two chunks per satellite. |
| `HISTORY_MAX_SPAN_DEG` | 60° | Oversized history boxes are shrunk to this around their centre rather than rejected. |
| `HISTORY_MIN_CELLS_ACROSS` | 150 | Coarsens history clustering for large areas, bounding response size regardless of the requested grid. |
| `HISTORY_CACHE_TTL_SECONDS` | 30 min | How long an assembled history response is reused. Stored as gzipped bytes, not Python objects. |
| `_HISTORY_SEMAPHORE` | 1 | History builds are serialized — they're by far the memory-heaviest thing the server does, and two continent-sized builds overlapping doubles the worst-case footprint on a 512MB box. |
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
| `HOTSPOT_COUNT` | 5 | Rows in the "Biggest fires right now" panel. |
| `HOTSPOT_MERGE_KM` | 250 | Hotspots closer than this are one fire complex for listing purposes; only the strongest is shown. |
| `HOTSPOT_MAX_PER_COUNTRY` | 2 | Cap on rows one country can occupy, so one huge fire season doesn't fill the whole list. |
| `SMOKE_PLUME_HOURS` | 24 | How far ahead the smoke plume is projected. |
| `SMOKE_ADVECTION_FRACTION` | 0.9 | Smoke travel speed as a fraction of the 10m wind — much higher than a fire front, which is limited by fuel. |
| `SMOKE_HALF_ANGLE` | 0.12 | Lateral plume widening per km travelled (~7° half-angle). |
| `SMOKE_DILUTION_SCALE_KM` | 60 | Distance scale over which concentration falls off as the plume widens. |
| `SMOKE_DEPOSITION_SCALE_KM` | 800 | Much longer scale for particles settling out of the air. |
| `SMOKE_MAX_SOURCES` | 5 | Strongest fire clusters inside the circle that get their own plume. |
| `HISTORY_MS_PER_DAY` | 1100 | Playback speed of the 7-day animation, in ms per day. |
| `HISTORY_MAX_HALF_SPAN_DEG` | 20 | Caps the region a history request covers. Kept tight because FIRMS' response time scales badly with area. |

---

## API reference

All endpoints are JSON. None require auth from the browser (the backend holds the only key needed, for FIRMS).

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/fires` | GET | Fire data. `bbox`, `grid`, `min_frp`, `min_confidence`, `min_hectares` query params filter/cluster it. `count_only=1` returns just the counts, skipping the fire list. Returns `ready: false` if the server hasn't finished its first fetch from NASA yet. |
| `/api/fires/history` | GET | The last `days` (2–7) of detections in a region, split by day, for the playback animation. `bbox` is required — a whole-world week is far too much data. Oversized boxes are clipped around their centre, and the response says so. |
| `/api/air-quality/batch` | POST | Ground-level PM2.5 and US AQI for a list of points. Returns nulls, never estimates, where no measurement is available. |
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
- **The Earth is round and your maths probably isn't.** Longitude wraps at ±180°, and any geometry that treats it as a flat number line silently produces wrong answers rather than errors — for us, half of Siberia registered as being in no country at all, and the country search for Russia returned nothing, for weeks, without a single exception being thrown. Worse, the obvious fix made *more* things wrong in ways we'd never have noticed without a broad sweep. Wrap-around bugs don't announce themselves.
- **Ask the API instead of guessing at it.** Two assumptions about NASA's history endpoint (that it accepts a 10-day range, and how a start date interacts with that range) were both settled in under a minute by sending it one request each. Both would have been silent, plausible-looking data bugs if we'd guessed — the first was wrong, the second wasn't.
- **Check the instrument before optimising against it.** A "cache hit" that looked like it took 5.7 seconds turned out to be the *measuring tool's* overhead; the actual request was 30 ms. We nearly optimised a problem that didn't exist — and then made the same mistake twice more before learning to reach for a raw HTTP client first.
- **Profile before optimising, because the bottleneck is rarely the interesting code.** Everyone's instinct for "why is clustering 455,000 fires slow" is the clustering. It was 10% of the time. The real costs were a generic serializer doing unnecessary work, and an entire expensive request that existed to display a single number. Measuring took ten minutes and pointed somewhere none of us would have guessed.
- **The fastest work is the work you don't do on the request path.** The biggest single win here wasn't making anything faster — it was noticing that the expensive result doesn't depend on the request at all, and building it on a timer instead so nobody waits for it.
- **The same rule can be right in one context and wrong in another.** Never grouping intense fires into clusters is correct for the live map and actively harmful for a week-long animation. A constant that encodes a judgement call should be a parameter, not a hardcoded assumption baked into a shared function.
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

### A 29-second first load, and where it actually went
The default world view took **29.4 seconds** to build on a cold cache. Rather than guess, we timed every stage of the request (`profile_load.py`, still in the repo). The result was not what any of us would have picked:

| Stage | Display request | Count request |
|---|---|---|
| filter 455k detections | 0.6s | 0.2s |
| cluster | 2.9s | 3.6s |
| **`jsonable_encoder`** | **2.4s** | **12.8s** |
| `json.dumps` | 0.5s | 2.9s |
| gzip | 0.3s | 2.6s |

Three separate problems, none of them the clustering everyone assumes is the bottleneck:

- **The most expensive request existed to display one number.** The "~96,594 active fires worldwide" line was fetching the entire clustered fire list — 7.7 MB, 86,000 clusters — to read `.count` off it and throw the rest away. Worse, it runs *concurrently* with the map's own load, so it wasn't just slow itself, it was starving the request the user was actually waiting on. A `count_only` flag skips building and serializing the list entirely: **22s → 0.03s**.
- **FastAPI's `jsonable_encoder` was the single most expensive step.** It recursively walks every value to work out how to serialize it — correct in general, and completely wasted on data that's already plain dicts of floats. Direct `json.dumps` does the same job for a quarter of the cost.
- **A cache miss did all the work twice** — once by hand to fill the cache, then again through FastAPI and GZipMiddleware to build the response. The slowest path was paying double. It now encodes once and returns that same buffer.

Then the actual fix: **none of this work depends on the request**, so the two responses every first-time visitor asks for are now pre-built on the refresh timer, off the request path. Combined: **29.4s → 0.39s**, and the page is interactive in under a second.

For the third time in this project, a "slow" measurement turned out to be the measuring tool — PowerShell's `Invoke-WebRequest` was adding ~70 seconds handling a 7 MB response body. Timed with a raw HTTP client the same request was under a second. We have started defaulting to the raw client.

### Half of Siberia didn't exist
Building the "Biggest fires right now" panel surfaced a bug that had been quietly live the whole time. The panel labels each hotspot by country, and the largest fires on Earth kept coming back as raw coordinates instead of "Russia".

The cause was that point-in-polygon treats longitude as a flat axis. The two countries in our border dataset whose outline crosses the antimeridian (Russia and Fiji) jump straight from +180° to −180°, so in flat lon/lat space the shape is torn in half and the ray-crossing count comes out wrong. The visible consequence was much bigger than a mislabelled list row: **searching "Russia" returned almost no fires**, because the country search filters every detection through that same test. It now returns 98,309.

The fix was to unwrap each ring *once* into a continuous longitude sequence — walk the vertices taking the shorter way round at every step — and cache that per ring, since the country search runs the test over thousands of fires against the same geometry.

Worth recording: the obvious one-line version of this fix (unwrap each vertex relative to the query point) is **worse than the bug**. It tears apart any ring more than 180° away from the point, which measured out as New Delhi resolving to Mexico and open Pacific ocean resolving to Uganda. Verified the real fix against 16 targeted locations plus a sweep of all 176 countries before trusting it.

### A week of data doesn't fit where a day does
The 7-day history hit three separate scaling walls that the live map never does, all found by measuring rather than reasoning:

- **The same caching mistake, twice.** Caching the assembled Python payload pushed memory to 543 MB, past the 512 MB hosting ceiling — the exact failure mode already fixed once for the world-view cache. Keeping only the gzipped response bytes brought it to 187 MB across six large regions.
- **A good rule in the wrong context.** Response size was dominated by the rule that fires above 27.4 MW always render individually rather than clustering. That's correct for the live map — a major fire shouldn't be visually diluted by small ones beside it — but a week of Siberia is tens of thousands of above-threshold detections *per day*, which is both illegible at that zoom and megabytes of JSON. History clusters everything; Russia's response went 2089 KB → 307 KB.
- **Precision nobody asked for.** Cluster centroids were serialized at full float precision for points representing grid cells tens of kilometres across. Rounding to 3 decimals (~110 m) was free.

A fourth "problem" turned out not to exist: a cache hit that appeared to take 5.7 seconds was PowerShell's `Invoke-WebRequest` building its response object. Timed with a raw HTTP client, the same request is 3–114 ms. Worth checking the instrument before optimising against it.

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
- Smoke concentrations downwind are modelled from a **single measured reading** at the fire plus the forecast wind — a Gaussian-ish dilution approximation, not an atmospheric dispersion model. It's a guide to where smoke goes, not a substitute for a local air quality monitor.
- The history animation is capped at 7 days and one region at a time. NASA's endpoint allows at most 5 days per request and its response time scales badly with area, so a whole-world week isn't practical on free hosting.
- A detection-age filter ("last 6h / 24h / 48h") and shareable URLs that encode the current view are both designed but unbuilt.
- User accounts and crowd-sourced fire reporting (via Supabase) were scoped and deliberately deferred — they need moderation and row-level security to be trustworthy, which is more than the remaining time allowed.
