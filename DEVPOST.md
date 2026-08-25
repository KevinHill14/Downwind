# Downwind

Paste-ready copy for the Devpost submission. Every number here was measured. If you edit them, re-measure first.

---

## Elevator pitch

*(Devpost's short tagline field, ~200 characters)*

> Every active wildfire on Earth, live on a spinning globe, plus where the wind is about to carry the smoke. Because most people a wildfire harms never see a flame.

---

## About the project

### Inspiration

Nearly every wildfire map answers one question: what is burning right now. That is the easy half, and it is the half that has already been built many times.

The questions people actually ask during a fire season are harder. Where is it going? Am I in danger? And the one almost nothing answers: is the air where I live about to become unbreathable?

That last question is the reason this project exists. The overwhelming majority of people a wildfire harms never see flame. They breathe smoke that has travelled hundreds of kilometres downwind, arriving in cities with no fire anywhere near them, while every distance-based tool on their phone reports that they are perfectly safe. I wanted to build the thing that answers all three questions on one screen, and I named it after the one it answers that nothing else does.

### What it does

- **A live 3D globe of every active fire on Earth**, refreshed every 10 minutes from three NASA VIIRS satellites plus Canada's ground-confirmed incident feed. Markers are graded by fire radiative power, from a small brush fire up through extreme and catastrophic.
- **Fire spread prediction** at four increasing tiers, from a single wind reading up to hourly wind that bends the projected path, terrain that biases spread uphill and downhill, and humidity, temperature and rainfall feeding into how fast it moves.
- **Uncertainty you can see.** A single projected line quietly implies the forecast wind is exact. Switch this on and the same spread maths re-runs 32 times per fire with the wind varied inside its real forecast error, and the region those runs land in is drawn in cyan underneath. A tight shape means the projection is trustworthy. A wide one means the wind could genuinely go several ways.
- **"Am I in danger?"** Type any address for a Safe, Watch or Danger verdict based on nearby fires and their predicted spread, with the nearest real threat's distance, confidence and estimated time of arrival, plus measured ground-level air quality reported separately from the fire verdict.
- **Smoke forecasting.** Circle any region and it finds every fire inside, reads the *measured* PM2.5 at the strongest ones, and projects where that smoke travels over the next 48 hours on forecast wind. The plume is drawn dark and opaque where the air is genuinely hazardous and fades to nothing where it has diluted back to background.
- **A 7-day replay.** One day of detections is a scatter of dots. The same region played day by day shows a fire front moving across the ground.

Plus a biggest-fires leaderboard, click-any-fire detail, prediction playback animation, country search that filters on real borders, and image export that saves straight to the camera roll on mobile.

### How I built it

**Backend:** FastAPI in Python, holding the entire world's fire dataset, roughly 500,000 detections, in memory and refreshing it on a timer. Every request is then a local filter over that array rather than a live call out to NASA, which is what makes panning the globe instant.

**Frontend:** one self-contained HTML file. Three.js and globe.gl for the globe, with all of the prediction maths, the smoke model and the rendering in the same file. No build step and no framework, which for a project this size meant every minute went into the actual problem.

**Data:** NASA FIRMS for VIIRS satellite detections, Natural Resources Canada CWFIS for ground-confirmed fires, Open-Meteo for wind, terrain and air quality, and OpenStreetMap Nominatim for geocoding.

**Hosting:** Render's free tier, whose 512 MB memory ceiling shaped a genuinely large share of the engineering decisions in this project.

Every fire marker on screen is one batched point-sprite draw call rather than thousands of individual objects, which is what keeps tens of thousands of them smooth on a phone.

### Challenges I ran into

**The fires a satellite cannot see are the big ones.** Thermal detection from orbit has a blind spot that gets worse exactly as a fire gets more serious, because a large established fire generates enough smoke to hide its own hottest core from a sensor looking straight down at it. My 7-day replay was satellite-only, and playing back Canada showed fires apparently thinning out over a week while they were actually intensifying. Watching your own animation quietly under-report a fire season is a good way to find a data problem. The fix was discovering that CWFIS publishes its reported-fires layer with a validity window on every record, which makes it a real time series: you can ask what the ground-confirmed picture was on a past day rather than filtering today's list. Merged in, I measured that on the smokiest days in eastern Canada roughly three quarters of all fire activity came from ground reports. The satellite-only replay had been showing about a third of reality.

**A rate limit that had nothing to do with me.** Predictions kept failing with `429 Too Many Requests` from the weather provider. I assumed I was calling too often, so I added backoff, then jitter, then serialised every outbound call, and it still failed. Production logs eventually showed retries backing off for over a minute and still getting refused, which proved this was sustained rather than a burst. The real cause was that my host's free tier shares outbound IP addresses across unrelated customers and the provider rate-limits by IP, so the quota was being consumed largely by someone else's traffic. No amount of tuning my own pacing could fix that. I stopped trying to prevent it and built graceful degradation instead, so a prediction always returns something.

**Half of Siberia did not exist.** Point-in-polygon maths treats longitude as a flat number line. Russia's outline crosses the antimeridian, jumping from +180 to -180, which tears the shape in half in flat coordinates. Every fire in Siberia, some of the largest on Earth, tested as being inside no country at all, and searching "Russia" returned almost nothing. This was live and silent for weeks without throwing a single exception. It now returns 98,309 detections. The obvious one-line fix turned out to be worse than the bug itself, resolving New Delhi to Mexico and open Pacific ocean to Uganda.

**Four out-of-memory restarts on a 512 MB box.** Three of them were the same mistake in three different places: caching assembled Python objects instead of compressed response bytes. The fourth was more interesting. Profiling put essentially all of the memory in the world dataset itself, where `str.split()` had given half a million detections their own private copies of values drawn from three confidence codes, seven dates and about 1,400 timestamps. Interning those three fields took the dataset from 171 MB to 112 MB and peak memory from 450 MB to 327 MB.

**A 29-second first load.** Rather than guess, I timed every stage. Everyone's instinct for why clustering 455,000 fires is slow is the clustering, and the clustering was 10% of it. The real costs were a generic serializer doing unnecessary work and an entire expensive request that existed to display a single number while starving the request the user was actually waiting on. Cold load is now 0.39 seconds and the globe is interactive in under one.

### What I learned

- **The Earth is round and your maths probably is not.** Wrap-around bugs return wrong answers instead of raising exceptions, so they can live in a codebase for weeks.
- **A rate limit is not always about your own traffic.** Shared infrastructure means someone else's usage becomes your outage, and retrying harder makes a sustained failure worse rather than better.
- **Profile before optimising, because the bottleneck is rarely the interesting code.** This was true for CPU and it was true again for memory. Both times the answer was a structure sitting in plain sight doing exactly what it was written to do.
- **Check the instrument before optimising against it.** Three separate times a "slow" measurement turned out to be my own measuring tool's overhead rather than the server.
- **Ask the API instead of guessing.** Two assumptions about NASA's history endpoint were settled in under a minute by sending one request each. One of them was wrong, and it would have been a silent, plausible-looking data bug.
- **The fastest work is the work that never happens on the request path.** The single biggest speed win came from noticing that an expensive result did not depend on the request at all, so it could be built on a timer instead.
- **A number a person might act on deserves different rules.** The air quality readout is the one thing in this app with no fallback. A guessed wind direction misaims a drawing on a globe. A guessed air quality figure is a health number someone might breathe on.

### Accomplishments I am proud of

**I built the tool that says my own model is not good enough.** `validate_predictions.py` measures the spread projection against what actually happened, scored against a persistence baseline of "predict the fire does not move", because fire detections cluster and any prediction lands near some fire.

It found that the projection does not beat persistence. Digging into why turned up something genuinely interesting: the ground truth itself is biased, because smoke blows downwind and obscures satellite detection there, so new detections skew upwind by 36 to 61 degrees from the reported wind against 90 degrees for random. That is a detection artifact rather than fire behaviour, and validating this properly needs mapped fire perimeters rather than point detections. I would rather ship that finding honestly than tune a constant until the graph looked good.

### What's next

- Crowd-sourced fire reporting with accounts, scoped and deliberately deferred because community reports need moderation and row-level security to be trustworthy.
- Validation against mapped fire perimeters, which is the only way to settle whether the spread model is any good.
- Wider ground-confirmed coverage. Canada has an excellent open incident feed and most countries do not.

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
