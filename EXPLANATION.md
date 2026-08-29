# Downwind, explained

A guided walkthrough of this codebase, written so you could explain the project to someone else afterwards. It assumes you can read code but have never opened this repo.

Read it top to bottom the first time. After that, section 9 is the one you will come back to.

---

## 1. What this project does

Most wildfire maps answer one question: **what is burning right now?** That is the easy half, and it has been built many times.

Downwind answers the question almost nothing else does: **is the air where I live about to become unbreathable?**

That matters because of a fact that shapes the entire design: the overwhelming majority of people a wildfire harms never see a flame. Smoke travels hundreds of kilometres and arrives in cities with no fire anywhere near them. Everyone there opens a wildfire map, sees no fire nearby, and is told they are perfectly safe while the sky turns orange.

So the app does four things:

1. **Shows every active fire on Earth** on a 3D globe, refreshed every 10 minutes.
2. **Projects where fires are heading**, using wind, terrain and weather.
3. **Projects where the smoke goes** over the next 48 hours.
4. **Answers "am I in danger?"** for a specific address, with *two* separate verdicts: one for fire, one for air.

That last point is the whole thesis. The address check can return **FIRE: SAFE** next to **AIR: UNHEALTHY** at the same time, because those are genuinely different risks with different responses. "Be ready to leave" and "stay indoors" are not the same instruction, so they never get merged into one score.

**Who uses it:** anyone in or downwind of a fire region who wants a straight answer about their own address. It was built solo for a hackathon, and it is explicitly not an operational tool. The UI says so, and so does this document.

---

## 2. High-level architecture

Two components. That is not an oversimplification, that is genuinely the whole system.

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
                                           |  /api/air-quality/forecast   |
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

There is no database, no build step, no framework on the frontend, and no separate worker process.

**The single most important architectural decision** is on the backend: it holds *the entire world's fire data in memory*, roughly 400,000 detections, refreshed on a timer. It does **not** fetch from NASA per request.

Why that matters: NASA's whole-world CSV takes 5 to 10 seconds to generate. If every pan of the globe triggered a fetch, the map would be unusable. Because the data is already resident, every `/api/fires` request is a list comprehension over memory. Dragging the globe feels instant because it is instant.

The cost of that decision is memory, and memory is the constraint that shaped a surprising amount of this codebase. See section 8.

---

## 3. How the pieces talk

### The main path: loading the map

```
browser                          backend                        NASA
   |                                |                             |
   |  GET /                         |                             |
   |------------------------------->|  (serves static/index.html) |
   |                                |                             |
   |  GET /api/fires?grid=0.5       |                             |
   |------------------------------->|                             |
   |                                |  already in memory,         |
   |                                |  already clustered,         |
   |                                |  already gzipped            |
   |<-------------------------------|                             |
   |  gzipped JSON of clusters      |                             |
   |                                |                             |
   |                    (every 10 minutes, in the background)     |
   |                                |---------------------------->|
   |                                |<----------------------------|
   |                                |  rebuild dataset, swap in   |
```

The response the browser gets is not raw detections. It is **clusters**, meaning fires within a grid cell merged into one FRP-weighted centroid:

```python
# main.py, _cluster_fires
groups: dict[tuple[int, int], list[dict]] = {}
for f in small_fires:
    key = (round(f["lat"] / grid_deg), round(f["lng"] / grid_deg))
    groups.setdefault(key, []).append(f)
```

Nothing is ever dropped. Every fire still contributes to a cluster, and fires above `LARGE_FIRE_MIN_FRP` (27.4 MW) skip grouping entirely so a major fire is never visually diluted by small ones next to it.

### The other path: everything else is a proxy

Wind, elevation, air quality and geocoding are all fetched by the **backend on the browser's behalf**, never by the browser directly. That is deliberate for three reasons:

- **Batching.** The frontend can ask for 500 wind points in one request, and the backend chunks them into groups of 40 for the upstream provider.
- **Caching.** A wind reading is reused for 15 minutes, elevation for 24 hours, air quality for 30.
- **Rate limiting.** All outbound weather calls pass through one semaphore, `_OPEN_METEO_CONCURRENCY = asyncio.Semaphore(1)`, so the app makes at most one outbound weather request at a time, across every user simultaneously.

### The key data structure

Everything flows around one small object:

```python
@dataclasses.dataclass(slots=True)
class Fire:
    lat: float
    lng: float
    confidence: str
    frp: float           # Fire Radiative Power, megawatts - the severity proxy
    acq_date: str
    acq_time: str
    ha: float | None = None   # hectares burned - ground-sourced fires only

    def __getitem__(self, key):   # so f["lat"] still works everywhere
        return getattr(self, key)
```

Two details here are load-bearing:

- `slots=True` removes the per-instance dictionary. Across 400,000 records that is a large amount of memory.
- `__getitem__` exists purely so that converting from plain dicts to a dataclass did not require touching every `f["lat"]` read site in the file. That is a migration convenience, deliberately kept.

`ha` is the tell for where a fire came from. Satellite detections leave it `None`; ground-confirmed Canadian fires carry real hectares.

---

## 4. Folder tour

The repo is deliberately flat. There are only ten tracked files.

```
Downwind/
├── main.py                  # the entire backend, 1,710 lines
├── static/
│   └── index.html           # the entire frontend, 4,445 lines
├── requirements.txt         # four dependencies
├── .env                     # your FIRMS_MAP_KEY (gitignored)
│
├── validate_predictions.py  # offline tool that grades the prediction model
├── profile_load.py          # offline tool that times startup
│
├── README.md                # the reference doc
├── DEVPOST.md               # hackathon submission copy
├── DEMO_SCRIPT.md           # 3-minute demo run sheet
└── ideas.txt                # scratch
```

**Why so flat?** This was a solo build under time pressure. Splitting `main.py` into modules would have bought import hygiene and cost navigation speed. The file is heavily sectioned with comments instead. Whether you would do this on a team project is a fair debate; for one person on a deadline it was the right call, and the code is honest about it.

**Why is the frontend one file?** No build step. No framework, no bundler, no `npm install`, no watch process. Edit the HTML, refresh the browser, see the change. Three.js and globe.gl come from a CDN. On a project this size, every hour not spent on tooling went into the actual problem.

---

## 5. The files that matter

### `main.py` (1,710 lines) — the backend, all of it

Read it in this order, because it is organised in roughly this order:

| Lines | What lives there |
|---|---|
| 79-95 | The `Fire` dataclass |
| 95-200 | Fetching from NASA FIRMS and Canada's CWFIS |
| 244-266 | `_dedupe_against_ground`, spatial dedupe between the two sources |
| 269-305 | `_refresh_world_fires`, the 10-minute heartbeat |
| 391-440 | `_cluster_fires`, the aggregation that makes responses small |
| 519-640 | `GET /api/fires`, the endpoint every visitor hits |
| 641-1090 | The 7-day history replay, including Canada's time-series merge |
| 1096-1450 | Wind and forecast proxying, retries, the concurrency semaphore |
| 1482-1640 | Air quality, current and 48-hour forecast |
| 1640-1710 | Geocoding, and serving the frontend |

### `static/index.html` (4,445 lines) — the frontend, all of it

Also sectioned. The section-header comments are your table of contents:

```
// --- Smoke plume geometry ---
// --- Fire spread predictions (gated by the Prediction toggle) ---
// --- Monte Carlo uncertainty sampling ---
// --- Prediction playback animation ---
// --- "Biggest fires right now" leaderboard ---
// --- "Am I in danger?" address check ---
// --- Last 7 days playback ---
// --- Air quality ---
// --- Smoke check (circle a region) ---
// --- Fire detail panel (click a marker) ---
// --- Save / share the current view as an image ---
```

Grep for `// --- ` and you have the map of the file.

### `validate_predictions.py` (413 lines) — the honesty tool

This is the most interesting file in the repo and it is not part of the app at all.

It rewinds to a past day, re-runs the spread projection using the wind that *actually blew*, and checks the result against the fires that really appeared afterwards. Crucially, it scores against **the laziest possible forecast: assume the fire does not move at all.** If wind, terrain and weather cannot beat standing still, they are not earning their place.

They did not beat it. That finding is in the README rather than hidden. Section 8 explains why the result is more subtle than "the model is bad".

### `requirements.txt` — four lines

```
fastapi==0.141.1
uvicorn[standard]==0.52.2
httpx==0.28.1
python-dotenv==1.2.2
```

That is the entire backend dependency tree. Pinned exactly, because a hackathon deploy that breaks on a transitive upgrade the night before submission is not a fun evening.

### `profile_load.py` (53 lines) — the stopwatch

Small, and it earned its place. First load was once 29 seconds. Rather than guessing which part was slow, this file timed each stage separately. The answer was counterintuitive (section 8), and first load is now 0.39 seconds.

---

## 6. The stack, and why

| Technology | Why it is here |
|---|---|
| **FastAPI** | Async is the actual requirement. The server spends nearly all its time waiting on NASA and Open-Meteo, so `asyncio.gather` over three satellite feeds costs the same wall-clock time as one. FastAPI also gives request validation for free via Pydantic, e.g. `lat: float = Field(ge=-90, le=90)`. |
| **httpx** | An async HTTP client, needed because `requests` would block the event loop. One shared client is reused for every outbound call so connections are pooled rather than reopened. |
| **uvicorn** | The ASGI server FastAPI runs on. Nothing exotic. |
| **python-dotenv** | Loads `FIRMS_MAP_KEY` from `.env`. One job. |
| **Three.js** | The 3D engine. Everything on the globe (markers, plumes, uncertainty footprints) is a Three.js object. |
| **globe.gl** | A wrapper that handles the sphere, the textures and the camera controls so the project did not have to. Using Three.js directly for a globe means writing that layer yourself. |
| **topojson-client + world-atlas** | Country borders, for search and labels. TopoJSON because it is dramatically smaller than the equivalent GeoJSON. |
| **Render** | Free hosting with GitHub auto-deploy. Its 512 MB memory ceiling shaped a genuinely large share of the engineering. See section 8. |

**Notably absent:** no database, no React, no bundler, no Redis, no Docker. The dataset rebuilds itself from NASA every 10 minutes, so persistence would add a moving part while buying nothing.

---

## 7. Running it locally

**Prerequisites:** Python 3.11+, and a free NASA FIRMS API key from <https://firms.modaps.eosdis.nasa.gov/api/> (takes a couple of minutes, no approval wait).

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

Run it:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.

**What to expect:** the server is ready almost immediately, and real fire data populates in the background a few seconds later. The map briefly shows 0 fires while NASA responds. That is intentional. The globe is handed over as soon as it exists rather than held behind a loading screen, and only the controls that genuinely need fire data stay dimmed.

Without `FIRMS_MAP_KEY`, the server still starts and serves the frontend, but `/api/fires` returns a clear 500 explaining the missing key rather than silently showing an empty map.

### Building

There is no build step. That is not an omission, it is the design. Edit `static/index.html`, refresh the browser.

### Testing

**There is no automated test suite, and you should know that going in.** Verification on this project was done three ways:

1. **Direct API probes.** Small throwaway Python scripts hitting endpoints and asserting on the shape and the numbers.
2. **Browser automation via Chrome DevTools Protocol.** Scripts that drive the real page, click the real buttons, and read the rendered text back out.
3. **`validate_predictions.py`**, which is the closest thing to a real test, since it grades the model against reality.

A quick manual smoke test after any change:

```bash
# is the world dataset loaded and clustering?
curl "http://127.0.0.1:8000/api/fires?grid=0.5" | head -c 300

# does the replay work over Canada, with ground data merged?
curl "http://127.0.0.1:8000/api/fires/history?bbox=-102,49,-95,60&days=7&grid=0.15" | head -c 300

# air quality, current and forecast
curl -X POST http://127.0.0.1:8000/api/air-quality/batch \
  -H "Content-Type: application/json" -d '[{"lat":49.28,"lng":-123.12}]'
curl "http://127.0.0.1:8000/api/air-quality/forecast?lat=49.28&lng=-123.12" | head -c 300
```

**A trap worth knowing:** if you automate the browser, disable the cache (`Network.setCacheDisabled`). More than once during development a test reported on the *previous* build and made a working change look absent.

---

## 8. The tricky parts

These are the bits that took real thought. They are also the bits most likely to confuse you if you meet them cold.

### The fires a satellite cannot see are the big ones

Satellites spot fires by looking down and detecting heat. A large, established fire makes so much smoke that it **covers its own hottest point**. So the bigger and more serious a fire gets, the better it hides.

This is invisible on the live map, where Canada's ground feed is merged in. It was glaring in the 7-day replay, which was satellite-only: playing back Canada showed fires apparently *dying down* over the week while they were in fact intensifying.

The fix came from Canada's ground crews. CWFIS publishes every agency-reported fire with a `record_start` / `record_end` validity window, which makes it a genuine time series. You can ask what was confirmed burning at noon on a past day, rather than seeing today's list filtered by a start date.

Then it turned out to be wrong a second time. Canada publishes the same fires through **two** feeds, and the one with the timestamps stops being updated mid-fire for several provinces. Manitoba gave it away: 112 fires on the live map, 3 in the replay. It now reads both feeds and merges them, deduplicated by `national_fire_id`.

### The air quality number is a model, not a sensor

This one is worth understanding properly, because it looks like a bug and is not.

Put the smoke tool right on top of an active fire and you can get back a *Good* AQI. That seems obviously wrong. The explanation is resolution. Sampling north from a 192 MW Ontario fire:

| Distance from fire | PM2.5 |
|---|---|
| 0 km | 11.3 |
| 6 km | 11.3 |
| 11 km | 11.3 |
| 22 km | 11.3 |
| 44 km | 8.3 |

The value does not move until 44 km. The provider is a **global atmospheric model on a roughly 40 km grid**, not a sensor at the fire. One fire's plume, averaged over 1,600 km², barely registers.

So the app never calls it "measured". It says *"Air at the fire's location"*, which is a real figure for a real area and a different claim. This is also precisely why the smoke projection exists: near a single fire the grid averages the plume away, and the model is what fills that gap.

### Every health number is either real or missing

Wind, terrain and elevation all fall back to an estimate when a provider fails, because a slightly wrong wind direction only misaims a drawing on a globe.

**Air quality has no fallback anywhere in the code.** If the reading is unavailable, the line and the badge disappear rather than guess. From the docstring:

```python
# Unlike the wind endpoints, this has NO synthetic fallback. A made-up
# wind direction only misaims a drawing on a globe; a made-up air quality
# number is a health figure someone might act on, and inventing one is
# worse than admitting we don't know.
```

### Half of Siberia did not exist

Searching "Russia" returned almost nothing, and some of the largest fires on Earth came back as being in no country at all.

Testing whether a point is inside a country treats longitude as a straight line from -180 to +180. Russia crosses the point where those two ends meet. On a globe that is one continuous border; on a straight line it is a country torn in half with a gap through the middle. Nothing ever crashed, because **a wrong answer is not an error**. Russia now returns 98,309 detections.

The obvious one-line fix was worse than the bug. It put New Delhi in Mexico and open Pacific in Uganda. The real fix (`unwrapRing`) walks each ring once, taking the shorter way round at every step, and was verified against 16 specific locations plus a sweep of all 176 countries before being trusted.

### The 512 MB ceiling, and where the memory actually went

Four out-of-memory restarts in production. Three were the same mistake in three places: caching the *finished, uncompressed* response instead of the compressed bytes actually sent. The rule the codebase now follows everywhere is **cache gzipped bytes, never assembled Python objects.**

The fourth was more interesting. Profiling found no leak. Almost all the memory was the fire data itself, and the reason was strings: splitting a CSV line gives every row its own separate copy of every value, so half a million fires each held a private copy of a date even though there were only seven distinct dates between them. `sys.intern()` pointed them all at one shared copy:

```python
confidence=sys.intern(cols[idx["confidence"]])
```

Dataset: 171 MB to 112 MB. Peak: 450 MB to 327 MB.

There is one memory spike that cannot be optimised away, and the code says so plainly:

```python
# The new dataset is fully built before it replaces the old one, so both
# are resident for an instant and this line is the high-water mark of the
# whole process - roughly double the steady state, every refresh.
_world_fires = satellite_fires + ground_fires
```

That is deliberate. The alternative is a window where requests see no fires at all.

### The rate limit that had nothing to do with us

Predictions kept failing with `429 Too Many Requests`. The obvious reading is "we are asking too often", so the fix went through four rounds: a concurrency cap, then randomised jitter, then full serialisation. It kept failing.

The cause was **where** the requests came from, not how many. Render's free tier sends outbound traffic through IP addresses shared with other customers, and Open-Meteo rate-limits per IP. Identical requests from a personal machine were never refused.

So the strategy changed from prevention to survival: a point whose weather cannot be fetched borrows the nearest point's real reading that did succeed. A prediction always returns something now.

### The model grades itself, and the result is subtle

`validate_predictions.py` asks whether fire grows the way the wind blows. For each fire it measures the direction new detections appeared in versus the wind direction. If growth were random, that angle would average about 90 degrees. It came out between 36 and 61, which is a strong relationship.

It was also pointing the **wrong way**. New fire appeared on the *upwind* side.

That says something about the satellite, not about fire. Smoke blows downwind and sits between the satellite and the ground, so downwind fires are exactly the ones the satellite cannot see. The fire is almost certainly spreading downwind as expected; the detections proving it are hidden under its own smoke.

Which means the data being graded against has a hole in it, **in precisely the place the model makes its prediction**. Tuning the model until that score improved would have taught it to predict fire spreading upwind, making it genuinely worse in order to match a flaw in the camera. Settling it properly needs ground-mapped fire perimeters rather than heat detections from orbit.

### The three recurring traps

If you are debugging here, check these before anything else:

1. **Cache gzipped bytes, never assembled Python objects.** Three OOM-shaped bugs, one cause.
2. **Measure the right process.** `Get-Process python | Select -First 1` picks the *measuring script*, not the server. Resolve the PID owning port 8000. This once produced a 42 MB reading for a server holding 500,000 records.
3. **Check the instrument before optimising against it.** Three separate times a "slow" result turned out to be the measuring tool, not the thing measured. The 29-second load was mostly a general-purpose converter doing unnecessary work on data already in the right shape. Clustering, which everyone assumed was the bottleneck, was 10% of it.

---

## 9. If I had to change X, where would I start

### "Add ground-confirmed fire data for another country"

This is the single biggest limitation, and the path is well worn because Canada already did it.

**Two integration points, both in `main.py`:**

1. **Live map.** Write `_fetch_<country>_fires()` returning a `list[Fire]`, mirroring `_fetch_canada_fires` at line 143. Set `confidence="h"`, derive `frp` from area with `max(30.0, math.log1p(hectares) * 15)` so it always renders as its own marker, and set `ha` to real hectares. Then add it to the gather in `_refresh_world_fires` (line 269).

2. **Replay.** Write `_fetch_<country>_history(start, end)` returning dicts of `{id, lat, lng, ha, start, end}`. Add a bounds check like `_bbox_intersects_canada` (line 641) so the request is skipped when the view is elsewhere, then wire it into the `canada_history()` gather inside `/api/fires/history`.

`_canada_fires_on_day` (line 751) is already generic. It takes records and a day and returns `Fire` objects, deduplicating by `id`, so it will work unchanged.

**The hard part is never the fetch. It is the validity window.** Canada publishes `record_start` / `record_end` directly. Most sources do not, and you have to construct one. This was actually built for the US (NIFC's WFIGS) and then reverted, and the lesson is worth repeating: a null "containment date" does **not** mean "still burning". Treating it that way put 11,942 phantom fires in a 7-day window, mostly long-finished incidents from January. Filtering on a "last modified" field instead is what separates active from stale.

**Do not forget** the attribution: the disclaimer bar and credits in `static/index.html` (around line 666), and the watermark in the image export (around line 4371).

### "Change how fire spread is predicted"

Everything lives in `static/index.html` under `// --- Fire spread predictions ---` (line 1808).

- `spreadDistanceKm(windSpeedKmh, frp, hours)` at line 1990 is the core formula. Start here.
- `runCountryPredictions` (line 2143) orchestrates: pick targets, batch wind lookups, build markers.
- `MAX_PREDICTIONS = 500` (line 1861) caps how many clusters get a projection.
- `WIND_GRID_DEG = 0.3` (line 1862) is what keeps it cheap. Every fire within ~30 km shares one wind lookup, so 500 targets collapse to about 311 actual requests.

**Before you change anything, run `validate_predictions.py`** to get a baseline, and read section 8 first. The scoring target has a known hole in it, so "the score improved" is not sufficient evidence that the model improved.

If you want to replace the heuristic with real fire science, the established approach is the Rothermel surface spread equations, and they need something this project does not have: fuel data, meaning what vegetation is on the ground, how dense it is, and how dry it is right now. The US publishes it through LANDFIRE; globally it is patchy.

### "Change what the address check says"

`static/index.html`, `// --- "Am I in danger?" address check ---` (line 2993).

- `checkAddressRisk()` (line 3176) is the orchestrator: geocode, fly the camera, fetch nearby fires, decide a verdict.
- `riskLevelFor(frp, distanceKm)` (line 3159) plus `RISK_THRESHOLDS_KM` are the actual decision rules. Note that the "yellow" severity tier is deliberately absent from the thresholds, because it never counts toward risk at any distance.
- `updateAddressAirQuality(lat, lng)` (line 3037) fills the air badge.
- `updateAddressAirForecast(lat, lng)` (line 3078) fills the 48-hour line.

**Two design rules to preserve if you touch this:**

1. The fire verdict and the air verdict never merge into one score. That separation is the entire point of the feature.
2. Air quality never falls back to a guess. If the fetch fails, the line and badge stay empty. Silence is the correct output for a health number you do not have.

Note also that the air calls are deliberately **not awaited**. The fire proximity check is the answer people came for, and it should not wait on, or be failed by, a second provider.
