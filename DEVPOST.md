# Downwind

Paste-ready copy for the Devpost submission. Every number here was measured. If you edit them, re-measure first.

---

## Elevator pitch

*(Devpost's short tagline field, ~200 characters)*

> Every active wildfire on Earth, live on a spinning globe, plus where the wind is about to carry the smoke. Because most people a wildfire harms never see a flame.

---

## About the project

### Inspiration

Nearly every wildfire map answers one question: what is burning right now? That is the easy half, and it has already been built many times.

The questions people actually ask during a fire season are harder. Where is it going? Am I in danger? And the one almost nothing answers: is the air where I live about to become unbreathable?

That last question is the reason this project exists. The overwhelming majority of people a wildfire harms never see flame. They breathe smoke that has travelled hundreds of kilometres, arriving in cities with no fire anywhere near them, while every distance-based tool on their phone reports that they are perfectly safe. I wanted one screen that answers all three questions, and I named it after the one that nothing else answers.

### What it does

- **A live 3D globe of every active fire on Earth**, refreshed every 10 minutes from three NASA VIIRS satellites plus Canada's ground-confirmed incident feed. Markers are graded by fire radiative power, from a small brush fire up through extreme and catastrophic.
- **Fire spread prediction** at four increasing tiers, from a single wind reading up to hourly wind that bends the projected path, terrain that biases spread uphill and downhill, and humidity, temperature and rainfall feeding into how fast it moves. Search a country and the 500 most intense fires in it get projected.
- **Uncertainty you can see.** A single projected line quietly implies the forecast wind is exact. Toggle "Show uncertainty" on and the spread maths re-runs 32 times per fire, with the wind varied inside its real forecast error each time, and the region those runs land in is drawn in cyan. A tight shape means the projection is trustworthy. A wide one means the wind could genuinely go several directions.
- **"Am I in danger?"** Type any address for a Safe, Watch or Danger verdict, based on nearby fires and their predicted spread, with the nearest real threat's distance, confidence and estimated time of arrival. Air quality gets its own badge beside the fire one, so the headline can read FIRE: SAFE and AIR: UNHEALTHY at the same time. That is exactly the situation most people downwind are in.
- **Smoke forecasting.** Circle any region and it finds every fire inside, reads the *measured* PM2.5 at the strongest ones, and projects where that smoke travels over the next 48 hours on forecast wind. The plume is drawn dark and opaque where the air is genuinely hazardous, and fades to nothing where it has diluted back to normal.
- **A 7-day replay.** One day of detections is a scatter of dots. The same region played day by day shows a fire front moving across the ground.

Additionally: a biggest-fires leaderboard, per-fire detail on click, prediction playback, country search that filters on real borders, and image export that saves straight to the camera roll on mobile.

### How I built it

**Backend:** FastAPI in Python, holding the entire world's fire dataset, roughly 500,000 detections, in memory and refreshing it on a timer. Every request is then a local filter over that array rather than a live call out to NASA, which is what makes panning the globe instant.

**Frontend:** one self-contained HTML file. Three.js and globe.gl for the globe, with all of the prediction maths, the smoke model and the rendering in the same file. No build step and no framework, which for a project this size meant every minute went into the actual problem. Every fire marker on screen is one batched point-sprite draw call rather than thousands of individual objects, which is what keeps tens of thousands of them smooth on a phone.

**Data:** NASA FIRMS for VIIRS satellite detections, Natural Resources Canada CWFIS for ground-confirmed fires, Open-Meteo for wind, terrain and air quality, and OpenStreetMap Nominatim for geocoding.

**Hosting:** Render's free tier, whose 512 MB memory ceiling shaped a genuinely large share of the engineering decisions in this project.

**Mobile:** one 820px breakpoint collapses the panels into icon-triggered overlays, and the JavaScript reads that same breakpoint back through `matchMedia`, so the CSS and the code can never disagree about what mobile means. Image export hands the file to the native share sheet, which is where "Save Image" lives on a phone.

### Challenges I ran into

**The fires a satellite cannot see are the big ones.** Thermal detection from orbit has a blind spot that gets worse exactly as a fire gets more serious, because a large established fire generates enough smoke to hide its hot core from a sensor looking straight down at it. My 7-day replay was satellite-only, and playing back Canada showed fires thinning out across the week while they were actually intensifying.

The fix was finding that CWFIS publishes its reported-fires layer with a validity window on every record, which makes it a real time series: you can ask what the ground-confirmed picture was on a past day, rather than filtering today's list. Merged in, ground reports turned out to supply 86 to 92% of fire activity in eastern Canada, so the satellite-only replay had been showing under a fifth of reality.

Then it was wrong a second time. CWFIS publishes the same fires through two separate products, and the versioned one that makes the time series possible stops updating mid-fire for several agencies. Manitoba gave it away, with 112 fires on the live map and 3 in the replay. It unions both products now.

**A rate limit that had nothing to do with me.** Predictions kept failing with `429 Too Many Requests` from the weather provider. I assumed I was calling too often, so I added backoff, then jitter, then serialised every outbound call, and it still failed. Production logs eventually showed retries backing off for over a minute and still being refused, which proved this was sustained rather than a burst.

The pattern pointed at the source IP rather than at my own pacing. My request volume sits far below the provider's published limits, and the same calls from a dedicated IP were never refused, even under heavier load than production saw. Render's free tier routes outbound traffic through shared addresses and the provider rate-limits by IP, so my own request rate was never the lever. I stopped trying to prevent it and built graceful degradation instead. A prediction always returns something now, which is why an outage would be invisible rather than fatal.

**Half of Siberia did not exist.** Searching "Russia" returned almost nothing, and some of the largest fires on Earth were coming back as belonging to no country at all. The cause is that point-in-polygon maths treats longitude as a flat number line, and Russia's outline crosses the antimeridian, jumping straight from +180 to -180. In flat coordinates that tears the country in half, so the test deciding which fires sit inside a border was running against a shape with a hole in it. It had been live for days without throwing a single exception, because a wrong answer is not an error. Russia now returns 98,309 detections. The obvious one-line fix was worse than the bug: it put New Delhi in Mexico, and a patch of open Pacific in Uganda.

**Four out-of-memory restarts on a 512 MB box.** Three of them were the same mistake in three different places: caching assembled Python objects instead of compressed response bytes. The fourth was more interesting. Profiling put essentially all of the memory in the world dataset itself, where `str.split()` had given half a million detections their own private copies of values drawn from three confidence codes, seven dates and about 1,400 timestamps. Interning those three fields took the dataset from 171 MB to 112 MB, and peak memory from 450 MB to 327 MB.

**Two phone bugs that a desktop never shows.** Three.js defaults its renderer pixel ratio to the device's own, which is harmless on a laptop and punishing on a phone: a 3x-DPI screen pays roughly nine times the fragment-shading cost of a capped 2x one, for sharpness that is invisible on a sphere this size. Capping it was the single biggest win for drag and orbit smoothness anywhere in the app. The second took longer to find. OrbitControls ships with no zoom limits, so a fast pinch could dolly the camera straight through the globe's surface, and between the near clip plane and the mesh the entire view went black, with no error to explain it. Both needed a real device to surface, because a mouse wheel cannot zoom fast enough to reproduce the second, and a 1x display never pays for the first.

**A 29-second first load.** Rather than guess, I timed every stage. Everyone's instinct for why clustering 455,000 fires is slow is the clustering, yet the clustering was 10% of it. The real costs were a generic serializer doing unnecessary work, and an entire expensive request that existed to display a single number while starving the request the user was actually waiting on. Cold load is now 0.39 seconds, and the globe is interactive in under one.

### Accomplishments I am proud of

**I built the tool that says my own model is not good enough.** `validate_predictions.py` rewinds to a past day, re-runs the projection using the wind that actually blew, and checks it against the fires that actually showed up afterwards.

The hard part is scoring it fairly. Fires burn in clusters, so a projected marker dropped almost anywhere near a fire will land close to *some* real detection, and a small average error proves nothing on its own. So the script scores against the laziest forecast available: assume the fire does not move at all. Beating that is the only result that means anything.

It does not beat it.

Then the interesting part. I measured which direction fires actually grew and compared it against the wind. If growth were random, the average angle between the two would sit at about 90 degrees. It came out between 36 and 61, and it pointed the wrong way: new detections cluster on the *upwind* side of a fire.

That turns out to be a fact about the satellite rather than about fire. Smoke drifts downwind and blocks the sensor's view of the ground beneath it, so downwind detections go missing and the surviving ones sit upwind. The data I am scoring against is therefore biased against the exact thing the model predicts, and no amount of tuning fixes that. Settling it properly needs mapped fire perimeters rather than individual hotspots.

I would rather ship that finding than quietly adjust a constant until the graph looked better.

**Every health number in the app is either measured or absent.** Wind, terrain and elevation all degrade gracefully when a provider fails, because an approximate wind direction only misaims a drawing on a globe. Air quality has no fallback anywhere in the codebase. If the measurement is missing, the line and the badge disappear rather than guess, because that is a number someone might act on.

### What I learned

- **A wrong answer is not an error.** The bugs that survived longest here were the silent ones: half a country missing from a search, a replay under-reporting an entire fire season, a green SAFE badge sitting over unbreathable air. None of them threw an exception. Each needed someone to look at the output and think it seemed off.
- **Check the instrument before optimising against it.** Three separate times, a "slow" measurement turned out to be my own measuring tool's overhead rather than the server.
- **Ask the API instead of guessing at it.** Two assumptions about NASA's history endpoint were settled in under a minute by sending one request each. One of them was wrong, and it would have become a silent, plausible-looking data bug.
- **The fastest work is the work that never happens on the request path.** The biggest single speed win in the project came from noticing that an expensive result did not depend on the request at all, so it could be built on a timer instead.

### What's next

- Crowd-sourced fire reporting with accounts, scoped and then deliberately deferred, because community reports need moderation and row-level security before they can be trusted.
- Validation against mapped fire perimeters, which is the only way to settle whether the spread model is any good.
- Wider ground-confirmed coverage. Canada publishes an excellent open incident feed. Most countries do not, and that gap is the single biggest limit on how far this can go.

---

## Built with

```
python
fastapi
uvicorn
javascript
three.js
globe.gl
webgl
html5
css3
nasa-firms
open-meteo
openstreetmap
nominatim
cwfis
geojson
render
```

---

## Submission checklist

- [ ] Screenshots: the globe with predictions running, an uncertainty footprint, the smoke plume, the "Am I in danger?" result
- [ ] Demo video (see `DEMO_SCRIPT.md`)
- [ ] Live URL and GitHub link
- [ ] Confirm the live deploy is serving the current commit before submitting
