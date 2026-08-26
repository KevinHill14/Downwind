# Downwind

![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688.svg)
![Frontend](https://img.shields.io/badge/frontend-Three.js%20%2F%20globe.gl-black.svg)
![Status](https://img.shields.io/badge/status-hackathon%20project-orange.svg)
![License](https://img.shields.io/badge/license-none-lightgrey.svg)

**Live: https://fire-tracker-gdwa.onrender.com/**

A 3D rotating globe of every wildfire burning on Earth right now, which also projects where those fires are heading and where the wind is about to carry their smoke.

Most wildfire maps answer "what is burning". The name of this one comes from the question it answers that they do not: the overwhelming majority of people a wildfire harms never see flame, they breathe smoke that has travelled hundreds of kilometres downwind, and distance-based tools tell them they are safe.

Fire data comes live from satellites (NASA FIRMS) and, for Canada, ground-confirmed agency reports (CWFIS). The globe is a single-page Three.js / `globe.gl` frontend. The backend is a FastAPI server that aggregates fire data and proxies weather, terrain and geocoding lookups.

---

## Table of contents

- [Features](#features)
- [Architecture](#architecture)
- [Running it locally](#running-it-locally)
- [Configuration](#configuration)
- [API reference](#api-reference)
- [Validating the prediction maths](#validating-the-prediction-maths)
- [What I learned](#what-i-learned)
- [Problems I ran into](#problems-i-ran-into)
- [Known limitations and ideas not built](#known-limitations-and-ideas-not-built)

---

## Features

### Live fire map

Every active fire detection worldwide, refreshed every 10 minutes from 3 NASA VIIRS satellites (SNPP, NOAA-20, NOAA-21) plus Canada's ground-confirmed active fire list.

Severity tiers run on Fire Radiative Power, a proxy for intensity: yellow, orange, red, then **extreme** (>=150 MW) and **catastrophic** (>=450 MW), log-scaled so the colour range stays meaningful across both a small brush fire and a megafire. Where markers overlap, the fiercest one is drawn on top, so a 4 MW smoulder can never sit over a 400 MW fire.

Canadian ground-confirmed fires are de-duplicated against satellite hits for the same physical fire, so a large complex shows as one ground marker rather than that marker plus a dense cloud of satellite pixels across its own footprint.

### Fire spread prediction

Select a country to project spread for its active fires, at 4 increasing tiers:

| Strength | What it adds |
|---|---|
| **Wind now** | One current wind reading, straight-line projected spread. |
| **Wind as it shifts** *(default)* | Wind re-sampled every 6 hours across the window, so the path visibly bends. |
| **Wind + terrain** | One elevation probe per fire. Spread is biased faster downhill and slower uphill. |
| **Wind, terrain + weather** | Terrain recomputed each step against that step's wind, plus humidity, temperature and precipitation feeding into speed. |

Each tier is named for what it feeds into the projection. The old names ("Basic", "Max accuracy") told you where a tier sat on a scale while saying nothing about the difference between them.

The window is adjustable from 1 to 7 days. This is explicitly a demo-grade approximation for visualising a plausible spread direction. It is not a physical fire-behaviour simulation and it should not drive real decisions.

**Show uncertainty** (off by default, available while prediction is on) re-runs the same spread maths 32 times per fire with the wind varied inside its realistic forecast error, and outlines the region those runs land in underneath the main projection. A tight outline means the wind is consistent and the projection is trustworthy. A wide one means it could genuinely go several ways. It costs no extra API calls, since it re-uses wind data already fetched, and every fire's footprint lives in one merged mesh so every marker gets one.

**Playback** walks every fire from its current position to its projected one across the window, so spread reads as motion. The button doubles as the clock (`Stop (+28h)`). Only markers move while the projected paths stay drawn underneath as the route, which keeps this to a single buffer update per frame instead of rebuilding hundreds of lines.

### Smoke forecast

Fire proximity is half of the danger. Smoke is the other half, and it travels hundreds of kilometres.

Click **Where's the smoke going?**, then click anywhere on the globe and pick a radius. The app finds every fire inside that circle, groups them into up to 24 smoke sources, reads the **measured** PM2.5 at the strongest ones, and projects where that smoke travels over the next 48 hours on forecast wind.

The plume's colour *and* opacity are set per-vertex from the US AQI at that distance, so it reads as dark and solid where the air is genuinely hazardous and fades to nothing where it has diluted back to background. A flat translucent shape would have shown where smoke goes while saying nothing about how bad it is on arrival.

The readout gives the **provider's published AQI** at the fire, the direction and reach of the plume, **estimated PM2.5** at distances spanning the whole plume, the point at which it thins back to ordinary air, and which countries it crosses. Three deliberate choices here:

- The source AQI is the provider's own published figure rather than one derived locally. US AQI is defined on an averaging window, so pushing an instantaneous PM2.5 through its breakpoints produces a different number from the official one. At a Ukrainian fire that difference was 55 against a published 69.
- Downwind figures are quoted in µg/m³ rather than as an AQI, which keeps a modelled number off the same scale as the measured one directly above it.
- Sample distances span the full plume rather than a fixed 50/150/300 km set, snapped to round numbers with the far tip always included. The fixed set stopped reporting at 300 km while the plume was still drawn out past 800 km, so the far half of what you could see on the globe carried no number at all. Scaling this costs nothing, since the concentration model is arithmetic and the geometry was already full length.
- A distance is only quoted while the model is still meaningfully above clean air. A weak source sits at background from the first step, and printing "50 km: ~5 (good); 150 km: ~5 (good); 300 km: ~5 (good)" dresses three identical numbers up as a measurement. That case now says the smoke blends into normal air almost immediately, and everything stronger also gets the distance at which it clears.

Concentration downwind is anchored on a real measurement at the source and then decayed for plume widening and deposition. Smoke advects at about 90% of wind speed, well above a fire *front*, which is limited by what it can burn through.

### "Am I in danger?" address check

Type an address. The app geocodes it, looks at nearby fires including their predicted spread, and returns a **Safe / Watch / Danger** badge with the nearest real threat's distance, confidence, and either how long ago it was detected or its predicted arrival time.

Ground-level air quality is reported alongside the fire verdict and never folded into it, because they are different risks with different responses. Without this, somebody 200 km downwind of a megafire got a confident **SAFE** while breathing hazardous air.

Unlike every wind endpoint, air quality has **no synthetic fallback**. A guessed wind direction misaims a drawing on a globe. A guessed air quality figure is a health number someone might act on. When the measurement is unavailable the line is omitted.

With **Show uncertainty** on, the check also reports how often a fire reaches you across sampled wind scenarios, for example *"100% of 32 wind scenarios bring a fire within watch range, 6% within danger range"*. If a substantial share of scenarios land worse than the single best-guess wind, the rating is raised to match and says so. A one-in-three chance of danger should not display as a flat SAFE just because the most likely wind happens to miss.

### Last 7 days replay

The live map answers "what is burning now". This answers "which way has it been going", which a single snapshot structurally cannot. One day of detections is a scatter of dots. The same region played day by day shows a fire front moving across the ground. It is also the only part of the app that is pure observation with no forecasting in it.

Press **Replay the last 7 days** to fetch the week for whatever region is on screen, then play, pause, or drag the scrubber to any single day. Leaving playback restores the live view exactly.

Over Canada it merges **ground-confirmed history** alongside the satellite record, reconstructed from CWFIS's time-series layer for each day. On smoky days the ground feed supplies most of the fire activity, because the biggest fires are the ones most likely to be hidden from a satellite by their own smoke. The day counter reads `Aug 23 · 3,151 fires`, with no breakdown by source: which feed a fire arrived on is an implementation detail, and nobody watching a week of fire move is asking it.

### Biggest fires right now

The globe opens on tens of thousands of markers with nowhere obvious to start, and every other feature only becomes reachable once you have picked somewhere to look. This panel is that entry point: the five places on Earth burning hardest, one click from view.

Ranked on its own fixed clustering grid rather than current display settings, so an unrelated Detail level change cannot alter what it ranks. Clusters within 250 km merge, and each country is capped at two rows. Without that cap, a bad week in Siberia filled all five rows with the word "Russia".

It also loads strictly last, after both the map and the worldwide count, so its request never competes with the thing you are actually looking at.

### Everything else

- **Click any fire** for position, detection count, total and peak FRP, area burned for ground-sourced fires, confidence, and detection recency. For a projected marker it gives lead time and the driving wind. Implemented by raycasting the batched marker cloud on click only, since hover would mean hit-testing every mouse move, which is what made an earlier attempt laggy.
- **Country search** flies the camera and loads only fires inside the real border, point-in-polygon filtered rather than a bounding box.
- **Save image** captures the globe as it looks and adds a caption bar with live fire count, timestamp and data credits. On mobile it hands off to the native share sheet, where "Save Image" lives.
- **Filters:** show small fires (off by default) and a 3-step Detail level (Sparse / Balanced / Max) controlling how aggressively nearby fires group. Nothing is ever dropped or capped by rank. Every fire still contributes to a cluster, and fires above a set intensity always render individually so a major fire cannot be diluted into one.
- **Mobile:** responsive below 820px. Search and settings collapse into icon-triggered overlays, the prediction and address panels share one screen slot, and touch targets resize.

### Loading behaviour

The globe is handed over as soon as it exists rather than held behind a loading screen. It can be panned and zoomed immediately, and only the controls that genuinely need world fire data dim until it lands, so a slow fetch reads as "this part isn't ready yet" rather than "the site is broken".

The two responses a first-time visitor always requests are pre-built on the data refresh timer, so nobody pays to construct them. Cold load of the default view went from **29.4s to 0.39s**.

If the server is still doing its own first fetch from NASA, `/api/fires` says so explicitly and the page waits it out, rather than drawing an empty globe that looks exactly like a world with no fires in it.

---

## Architecture

```
+-------------------------------+          +------------------------------+
|       static/index.html       |   HTTP   |           main.py            |
|     (single-file frontend)    |--------->|      (FastAPI backend)       |
|                               |          |                              |
|  Three.js + globe.gl          |<---------|  /api/fires                  |
|  all UI, prediction maths,    |   JSON   |  /api/fires/history          |
|  smoke model, rendering       |          |  /api/wind/batch             |
+-------------------------------+          |  /api/wind/forecast/batch    |
                                           |  /api/elevation/batch        |
                                           |  /api/air-quality/batch      |
                                           |  /api/geocode                |
                                           +------------------------------+
                                                           |
                         +---------------------------------+---------------------------------+
                         v                                 v                                 v
           +--------------------------+      +--------------------------+      +--------------------------+
           |    NASA FIRMS (VIIRS)    |      |      CWFIS (Canada)      |      |  Open-Meteo / Nominatim  |
           |  satellite detections    |      |  ground-confirmed fires  |      |  wind, elevation, air    |
           |  free API key needed     |      |  no key needed           |      |  quality, geocoding      |
           +--------------------------+      +--------------------------+      +--------------------------+
```

The backend holds the *entire world's* fire data in memory, refreshed on a timer rather than fetched per request, so a browser panning anywhere on the globe is a fast in-memory filter with no round-trip to NASA. Weather and terrain lookups are proxied through the backend rather than called from the browser, so they can be batched, cached and rate-limited server-side.

---

## Running it locally

### Prerequisites

- Python 3.11+
- A free NASA FIRMS API key from **https://firms.modaps.eosdis.nasa.gov/api/** (a couple of minutes, no approval wait)

### Setup

```bash
git clone https://github.com/KevinHill14/Downwind.git
cd Downwind

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

Then open **http://127.0.0.1:8000**.

The server is ready almost immediately and real fire data populates in the background a few seconds later. The map briefly shows 0 fires while NASA's response arrives, which is intentional.

> Without `FIRMS_MAP_KEY` set, the server still starts and serves the frontend, but `/api/fires` returns a clear 500 explaining the missing key instead of silently showing an empty map.

---

## Configuration

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `FIRMS_MAP_KEY` | Yes | Free NASA FIRMS API key. Without it, fire data cannot be fetched at all. |

### Backend constants (`main.py`)

| Constant | Value | Controls |
|---|---|---|
| `WORLD_REFRESH_INTERVAL_SECONDS` | 10 min | How often the world's fire data is re-fetched. Matches FIRMS' own near-real-time cadence. |
| `FORECAST_CACHE_TTL_SECONDS` | 15 min | How long a wind reading for a point is reused. |
| `ELEVATION_CACHE_TTL_SECONDS` | 24 hr | Terrain cache. Long, since terrain does not change. |
| `AIR_QUALITY_CACHE_TTL_SECONDS` | 30 min | Longer than wind because air quality is only published hourly. |
| `CACHE_EVICTION_INTERVAL_SECONDS` | 2 min | How often expired entries are swept out of memory. |
| `MAX_BATCH_POINTS` | 1000 | Cap on points per wind/elevation/forecast request. |
| `_WORLD_VIEW_CACHE_MAX_ENTRIES` | 8 | Max cached world-view responses (LRU). Bounds cache memory. An unbounded version of this caused an out-of-memory restart in production. |
| `_DEFAULT_VIEW_GRID` / `_DEFAULT_VIEW_MIN_FRP` / `_ESTIMATE_GRID` | 0.5 / 3.0 / 0.05 | The exact request a first-time visitor makes. Both responses are pre-built on every refresh. Keep in sync with the frontend defaults. |
| `_OPEN_METEO_CONCURRENCY` | 1 | Outbound weather calls in flight at once, across every user. Fully serialised on purpose. |
| `_FIRES_COMPUTE_SEMAPHORE` | 20 | How many `/api/fires` requests can do real filter/cluster work before new ones get a fast "busy, retry" response instead of queuing forever. |
| `LARGE_FIRE_MIN_FRP` | 27.4 MW | Fires at or above this always render as their own marker. History deliberately overrides this. |
| `GROUND_DEDUP_RADIUS_DEG` | 0.15° (~15-17 km) | How close a satellite detection must be to a ground-confirmed fire to count as the same fire. |
| `HISTORY_MAX_DAYS` | 7 | Longest history window. FIRMS caps one request at 5 days, so 7 days is fetched as two chunks per satellite. |
| `HISTORY_MAX_SPAN_DEG` | 60° | Oversized history boxes are shrunk around their centre rather than rejected. |
| `HISTORY_MIN_CELLS_ACROSS` | 150 | Coarsens history clustering for large areas, bounding response size regardless of requested grid. |
| `HISTORY_CACHE_TTL_SECONDS` | 30 min | How long an assembled history response is reused. Stored as gzipped bytes rather than Python objects. |
| `_HISTORY_SEMAPHORE` | 1 | History builds are serialised. They are the memory-heaviest thing the server does, and two continent-sized builds overlapping doubles the worst case on a 512 MB box. |

### Frontend constants (`static/index.html`)

| Constant | Value | Controls |
|---|---|---|
| `DETAIL_GRID_STEPS` | `[1.5, 0.5, 0.1]` | The 3 clustering grids the Detail level slider steps through. The middle value is the default and **must** match `_DEFAULT_VIEW_GRID`, or every first page load becomes a cold build again. |
| `FRP_SCALE_REF` | 150 MW | The FRP value colour intensity is normalised against. |
| `CATASTROPHIC_FRP_THRESHOLD` | 450 MW | Threshold for the darkest marker colour. |
| `COUNTRY_LABEL_CAP` | 45 | Max place-name labels rendered at once. |
| `LABEL_VISIBILITY_FACTOR` | 6 | A label appears once camera altitude is within 6x that feature's natural framing altitude. |
| `WIND_GRID_DEG` | 0.3° (~30 km) | Fire clusters within this distance share one wind lookup, which is what keeps the projection cheap. |
| `MAX_PREDICTIONS` | 500 | Fire clusters given a projection, most intense first. Canada has ~3,000, so a low cap made predictions look broken rather than capped. Costs far less than the number suggests: 500 targets collapse to 311 distinct wind points, sent 40 at a time. |
| `MONTE_CARLO_SAMPLES` | 32 | Wind scenarios simulated per fire when "Show uncertainty" is on. |
| `MC_BEARING_SD_DEG` | 22° | Assumed scenario-level wind direction uncertainty. |
| `MC_SPEED_SD_FRAC` | 0.28 | Assumed wind speed uncertainty (about ±28%). |
| `MC_FOOTPRINT_ALTITUDE` | 0.006 | Height of the uncertainty footprint, under the projected path. |
| `MC_RISK_ESCALATION_FRACTION` | 0.30 | Share of scenarios that must reach a risk level before the address check raises its rating. |
| `PREDICTION_ANIMATION_DURATION_MS` | 7000 | How long playback takes to cover the whole window. |
| `FIRE_CLICK_TOLERANCE_PX` | 12 | Click tolerance for selecting a marker, in screen pixels. |
| `HOTSPOT_COUNT` / `HOTSPOT_MERGE_KM` / `HOTSPOT_MAX_PER_COUNTRY` | 5 / 250 km / 2 | Leaderboard rows, the distance at which two hotspots count as one complex, and the per-country row cap. |
| `SMOKE_PLUME_HOURS` | 48 | How far ahead the smoke plume is projected. |
| `SMOKE_MAX_SOURCES` | 24 | Fire groups inside the circle that each get their own plume. |
| `SMOKE_ADVECTION_FRACTION` | 0.9 | Smoke travel speed as a fraction of 10 m wind, far above a fire front, which is limited by fuel. |
| `SMOKE_HALF_ANGLE` | 0.12 | Lateral plume widening per km travelled (about a 7° half-angle). |
| `SMOKE_DILUTION_SCALE_KM` / `SMOKE_DEPOSITION_SCALE_KM` | 60 / 800 | Distance scales for concentration falling off as the plume widens, and for particles settling out. |
| `HISTORY_MS_PER_DAY` | 1100 | Replay speed in ms per day. |
| `HISTORY_MAX_HALF_SPAN_DEG` | 20 | Caps the region a history request covers. Kept tight because FIRMS' response time scales badly with area. |

---

## API reference

All endpoints return JSON. None require auth from the browser, since the backend holds the only key needed.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/fires` | GET | Fire data. `bbox`, `grid`, `min_frp`, `min_confidence`, `min_hectares` filter and cluster it. `count_only=1` returns just the counts. Returns `ready: false` if the server has not finished its first NASA fetch. |
| `/api/fires/history` | GET | The last `days` (2-7) of detections in a region, split by day. `bbox` is required, since a whole-world week is far too much data. Oversized boxes are clipped around their centre and the response says so. Over Canada it merges CWFIS ground history (`has_ground_data`, per-day `ground_count`). |
| `/api/air-quality/batch` | POST | Ground-level PM2.5 and US AQI for a list of points. Returns nulls rather than estimates where no measurement exists. |
| `/api/wind/batch` | POST | Current wind for a list of `{lat, lng}` points. |
| `/api/wind/forecast/batch` | POST | Hourly wind, humidity, precipitation and temperature over `hours` hours. |
| `/api/elevation/batch` | POST | Ground elevation for a list of points. |
| `/api/geocode` | GET | Place name or address (`q`) to `{lat, lng, name, ...}`. |

---

## Validating the prediction maths

`validate_predictions.py` measures the spread projection against what actually happened. It takes fires as they were on a past day, re-runs the same projection using the wind that really blew, and compares against the detections that followed.

```bash
python validate_predictions.py --bbox=-10,36,4,44 --days-ago 4 --hours 24
```

It scores against a **persistence baseline** ("predict the fire does not move"), because fire detections cluster, so any prediction lands near some fire and absolute error alone means nothing. It also sweeps the spread-rate constant and tests whether wind direction carries signal.

**What it found, honestly:** the projection does *not* beat persistence, and error grows the further it projects. That looks damning until you look at the ground truth, which turns out to be biased. Smoke blows downwind and obscures satellite thermal detection there, so detections are systematically missing exactly where spread is predicted. Measured directly, new detections skew **upwind**, 36 to 61 degrees from reported wind against 90 degrees for random. That is a detection artifact rather than fire behaviour, and the app's handling of the wind convention is correct.

The honest conclusion is that point detections cannot cleanly validate a spread model. That needs mapped fire perimeters. The script stands as a regression check on the maths and a sanity check on magnitudes, and it is deliberately written to argue against its own headline number rather than quote it.

---

## What I learned

- **The Earth is round and your maths probably is not.** Longitude wraps at ±180°, and geometry that treats it as a flat number line produces wrong answers silently instead of raising errors. Half of Siberia registered as being in no country for weeks without a single exception. The obvious fix made *more* things wrong in ways I would never have noticed without a broad sweep.
- **A rate limit is not always about your own traffic.** Shared hosting means outbound IPs are shared, and a provider that rate-limits by IP can be exhausted by strangers. No amount of tuning my own pacing fixes a quota someone else is using.
- **Retrying is the wrong instinct against a sustained failure.** It is correct for a brief burst and actively harmful otherwise, since every retry adds load to something already overwhelmed. Telling the two apart required real production logs.
- **Profile before optimising, because the bottleneck is rarely the interesting code.** Everyone's instinct for why clustering 455,000 fires is slow is the clustering. It was 10% of the time. The same lesson repeated for memory, where the answer was a data structure sitting in plain sight doing exactly what it was written to do.
- **Check the instrument before optimising against it.** Three separate times, a "slow" measurement was my measuring tool's overhead. A cache hit that looked like 5.7s was really 30ms.
- **The fastest work is the work that never happens on the request path.** The biggest single win came from noticing that an expensive result did not depend on the request, so it could be built on a timer.
- **Batching matters more than almost anything else.** Every weather and terrain lookup batches many points per HTTP request. That is the difference between a prediction taking 1 second and 40. Free APIs also tend to rate-limit by request count rather than data volume, so fewer, bigger calls is close to free performance.
- **A synchronous loop over tens of thousands of items blocks everything on a single-threaded server**, including unrelated, otherwise-instant requests. This hides on a fast local machine and only appears under real concurrency.
- **Cache the fully-rendered output, not the raw data.** Caching a Python object still leaves you re-serialising and re-compressing on every request. Caching the pre-gzipped response bytes is what removes the cost.
- **The same rule can be right in one context and wrong in another.** Never clustering intense fires is correct for the live map and actively harmful for a week-long animation. A constant that encodes a judgement call should be a parameter.
- **Geospatial dedup and clustering are both "put things in grid cells" in disguise.** Rounding a coordinate to a grid key gets you 90% of the way there for a fraction of the complexity of a real spatial index.
- **A number someone might act on deserves different rules from a number they only look at.** Hence air quality having no fallback anywhere in the app.
- **Tests can lie in the same direction as your hopes.** Three separate times a test passed while the feature was broken, because it measured something adjacent to the real thing: `getBoundingClientRect` on a clipped element, a fixed sleep instead of waiting for a camera to land, and a mid-load race. A test that cannot fail for the reason you care about is worse than no test, because it buys false confidence.

---

## Problems I ran into

### The fires a satellite cannot see are the big ones

Satellite thermal detection has a blind spot that gets worse exactly as a fire gets more serious. A large, well-established fire generates enough smoke to obscure its own hottest core from a sensor looking straight down. So the fires most worth showing are the ones most likely to be missing.

This is invisible on the live map, where Canada's ground feed is already merged in. It was glaring in the 7-day replay, which was satellite-only. Playing back Canada showed fires *thinning out* across the week while they were in fact intensifying.

The fix came from finding that CWFIS publishes its reported-fires layer through a GeoServer WFS endpoint where every record carries a `record_start` / `record_end` validity window. That makes it a genuine time series: asking which records were valid at noon on a past day returns the ground-confirmed picture **as it stood then**, rather than today's list filtered by a start date.

One request covers the whole window and days are bucketed locally. I verified that against per-day queries across a full 7-day window before relying on it: identical on every day, 0 missing and 0 extra, for a seventh of the requests.

How much it mattered, measured:

| Region | Satellite/day | Ground/day | Ground share |
|---|---|---|---|
| Quebec / Ontario | 37-74 | 451-456 | **86-92%** |
| Manitoba | 8-53 | 111-113 | **68-93%** |
| BC / Alberta | 73-2123 | 174-240 | **8-77%** |

In eastern Canada the ground feed supplies the overwhelming majority of fire activity, and a satellite-only replay was showing well under a fifth of reality. BC and Alberta swing widely, since a clear day there produces thousands of satellite detections and a smoky one produces almost none, which is the blind spot restating itself.

### One ground feed was not enough

The numbers above are larger than the ones this section first carried, because the first version of this merge was itself incomplete. CWFIS publishes the same fires through two products, and the versioned layer that makes the time series possible **stops being updated mid-fire for several agencies**. Measured on 2026-08-26, currently-burning fires that the versioned layer also had a record for that day:

| | | | | | |
|---|---|---|---|---|---|
| MB **2 of 142** | NL **0 of 22** | AB **0 of 12** | QC 33 of 192 | SK 7 of 48 | BC 9 of 38 |
| ON 82 of 178 | NT 161 of 185 | PC 21 of 25 | YT 14 of 22 | NS 1 of 2 | NB 0 of 2 |

Manitoba was the tell. The live map showed 112 fires there and the replay showed 3, which is what surfaced the bug. The replay now unions both products, deduplicated by `national_fire_id`, which recovers around 530 fires a day nationally. Neither product is sufficient alone: the versioned layer goes stale mid-fire, and the current list cannot describe a fire that has already gone out.

### Predictions, which took the most iteration by far

The prediction feature depends on a free third-party weather API, and getting it *reliable* was the hardest problem here. It went through four rounds, each fixing something real but incomplete:

1. **A burst of simultaneous requests** tripped the rate limit outright. Added a concurrency cap so requests queue.
2. **Rate-limited chunks retried in lockstep**, colliding again on every attempt, so the backoff maths never mattered. Added randomised jitter.
3. **Predictions for large countries still failed.** Production logs showed retries backing off for over a minute and still being refused, which proved this was sustained rather than bursty. The cause was the host's free tier sharing outbound IPs across unrelated customers against a provider that rate-limits by IP. Fully serialised outbound weather calls and shortened the retry budget, since retrying into a sustained external outage delays failure rather than preventing it.
4. **An external shared-IP limit can never be guaranteed not to happen.** Added a two-level fallback: a point whose weather could not be fetched borrows the nearest point's real reading that did succeed, and if nothing at all succeeded it falls back to a plausible synthetic default. A prediction now always returns a marker for every point, trading exact precision for an approximation instead of failing visibly.

The broader lesson: the first three fixes were all genuine improvements, each solving one layer of a problem with another layer underneath, discoverable only against real production logs. A personal, non-shared IP never reproduced the underlying issue at all.

### Half of Siberia did not exist

Building the leaderboard surfaced a bug that had been live the whole time. The panel labels each hotspot by country, and the largest fires on Earth kept coming back as raw coordinates.

Point-in-polygon treats longitude as a flat axis. The two countries in the border dataset whose outlines cross the antimeridian (Russia and Fiji) jump from +180° to -180°, so in flat lon/lat space the shape tears in half and the ray-crossing count comes out wrong. The visible consequence was far bigger than one mislabelled row: **searching "Russia" returned almost no fires**, because country search filters every detection through that same test. It now returns 98,309.

The fix unwraps each ring *once* into a continuous longitude sequence, walking vertices and taking the shorter way round at every step, cached per ring since the search runs the test over thousands of fires against the same geometry.

Worth recording: the obvious one-line version (unwrap each vertex relative to the query point) is **worse than the bug**. It tears apart any ring more than 180° from the point, which measured out as New Delhi resolving to Mexico and open Pacific resolving to Uganda. I verified the real fix against 16 targeted locations plus a sweep of all 176 countries before trusting it.

### A 29-second first load, and where it actually went

The default world view took **29.4 seconds** on a cold cache. Rather than guess, I timed every stage (`profile_load.py`, still in the repo):

| Stage | Display request | Count request |
|---|---|---|
| filter 455k detections | 0.6s | 0.2s |
| cluster | 2.9s | 3.6s |
| **`jsonable_encoder`** | **2.4s** | **12.8s** |
| `json.dumps` | 0.5s | 2.9s |
| gzip | 0.3s | 2.6s |

Three problems, none of them the clustering everyone assumes is the bottleneck:

- **The most expensive request existed to display one number.** The "~96,594 active fires worldwide" line was fetching the entire clustered list, 7.7 MB and 86,000 clusters, to read `.count` off it and discard the rest. It also ran *concurrently* with the map's own load, so it was starving the request the user was waiting on. A `count_only` flag skips building and serialising the list: **22s to 0.03s**.
- **FastAPI's `jsonable_encoder` was the single most expensive step.** It recursively walks every value to decide how to serialise it, which is correct in general and completely wasted on plain dicts of floats. Direct `json.dumps` does the same job for a quarter of the cost.
- **A cache miss did all the work twice**, once by hand to fill the cache and again through FastAPI and GZipMiddleware to build the response. It now encodes once and returns that same buffer.

Then the actual fix: **none of this work depends on the request**, so the two responses every first-time visitor asks for are pre-built on the refresh timer, off the request path entirely. Combined: **29.4s to 0.39s**, with the page interactive in under a second.

The same "stop competing with the thing being looked at" rule governs load *order*. The biggest-fires list is its own fetch on a fixed 0.3° grid, and the server runs on one core, so it now goes strictly last. Switched on mid-load it queues and says "Still loading" rather than firing immediately. Almost nobody flips that toggle inside the couple of seconds the rest of the load takes, and anyone who does would rather have the globe first.

### A week of data does not fit where a day does

The 7-day history hit three scaling walls the live map never does, all found by measuring:

- **The same caching mistake, twice.** Caching the assembled Python payload pushed memory to 543 MB, past the 512 MB ceiling, which was the exact failure mode already fixed once for the world-view cache. Keeping only gzipped response bytes brought it to 187 MB across six large regions.
- **A good rule in the wrong context.** Response size was dominated by the rule that fires above 27.4 MW always render individually. That is correct for the live map, where a major fire should not be diluted by small ones beside it. A week of Siberia is tens of thousands of above-threshold detections *per day*, which is both illegible at that zoom and megabytes of JSON. History clusters everything, and Russia's response went from 2089 KB to 307 KB.
- **Precision nobody asked for.** Cluster centroids were serialised at full float precision for points representing grid cells tens of kilometres across. Rounding to 3 decimals (about 110 m) was free.

### Half a million private copies of the word "2026-08-24"

Production kept restarting on the 512 MB ceiling. The caches were the obvious suspects, and twice before they had genuinely been the cause, but they came back innocent at 0.6 MB. Profiling each startup stage put essentially all of it in one place: `_world_fires`, the in-memory world dataset, at **171 MB for 498,494 detections**.

The records were already `@dataclass(slots=True)`, so per-object overhead was gone. What remained was strings. `str.split()` returns a **brand new string object for every field of every row**, so half a million detections held half a million private copies of values drawn from *three* confidence codes, *seven* dates and about 1,400 timestamps. `sys.intern()` on those three fields collapses each set to one shared object:

| | before | after |
|---|---|---|
| bytes per record | 359 | **235** |
| `_world_fires` (498,494 records) | 171.4 MB | **112.0 MB** |
| steady-state RSS | 260.9 MB | **193.1 MB** |
| **peak RSS across a refresh** | **450.6 MB** | **326.9 MB** |

That peak is the number that mattered. Every refresh builds the complete new dataset *before* swapping it in, so both are resident for an instant. That is deliberate, since the alternative is a window where requests see no fires at all, and it means the process high-water mark is roughly double the dataset and every byte saved per record is saved twice. At 450 MB against a 512 MB ceiling, restarts were happening with **no traffic at all**. The margin is now 185 MB.

### Rendering tens of thousands of markers without lag

Two layers, two different fixes:

- **Server-side:** clustering means a dense region returns a few hundred aggregate points instead of tens of thousands of raw ones. Nothing is dropped, since every fire still contributes to a cluster's position and intensity. Separately, clustering is real CPU work with no natural `await` point, and running it in the request handler froze the *entire server* for every other concurrent user. Load testing found that. Moving it to a background thread fixed it without touching the clustering itself.
- **Client-side:** all markers are one batched point-sprite draw call. Country and ocean labels turned out to be the most expensive per-frame work in the app, so they are hidden during camera motion and rebuilt once it settles, which was the single biggest win for drag and zoom smoothness. Capping pixel ratio on high-DPI screens, where GPU cost scales with pixel count rather than visible sharpness, was another large and cheap win.

---

## Known limitations and ideas not built

- Prediction is a **demo-grade approximation**, useful for visualising plausible direction and reach, unsuitable for operational or safety decisions.
- Ground-confirmed fire data covers **Canada only**. Most countries do not publish an equivalent open incident feed.
- Smoke concentrations downwind are modelled from a **single measured reading** at the fire plus forecast wind, which is a Gaussian-ish dilution approximation rather than an atmospheric dispersion model. It guides you to where smoke goes and does not replace a local air quality monitor.
- The replay is capped at 7 days and one region at a time. NASA's endpoint allows at most 5 days per request and its response time scales badly with area, so a whole-world week is impractical on free hosting.
- Ocean name labels reuse the country label system and are not fully built out.
- A detection-age filter and shareable URLs encoding the current view are both designed and unbuilt.
- User accounts and crowd-sourced reporting were scoped and deliberately deferred, since community reports need moderation and row-level security to be trustworthy, and doing that badly is worse than not doing it.
- A compass showing camera orientation and a "jump to my location" button were discussed and not built.
