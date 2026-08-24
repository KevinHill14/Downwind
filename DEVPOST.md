# Fire Tracker — Devpost draft

Paste-ready copy for the Devpost submission, one section per Devpost field.
Numbers here are real and measured; if you edit them, re-check them first.

---

## Elevator pitch
*(Devpost's short tagline field, ~200 characters)*

Every active wildfire on Earth on a 3D globe — where they are, where the wind will take them, and whether the smoke is heading for you.

---

## Inspiration

Most wildfire maps answer one question: *what is burning right now?* That's the easy half.

The questions people actually have during a fire season are harder. **Where is it going?** **Am I in danger?** And the one almost nothing answers — **is the air where I live about to become unbreathable?** The overwhelming majority of people a wildfire harms never see flame. They breathe smoke that travels hundreds of kilometres downwind, and every distance-based tool tells them they're perfectly safe.

We wanted to build the thing that answers all three.

---

## What it does

**A live 3D globe of every active fire on Earth**, refreshed every 10 minutes from three NASA VIIRS satellites plus Canada's ground-confirmed incident feed. Fires are colour-graded by radiative power, from a small brush fire up through *extreme* and *catastrophic*.

**Fire spread prediction**, at four increasing tiers of accuracy — from a single wind reading up to hourly wind that bends the projected path, terrain that biases spread uphill and downhill, and humidity, temperature and rainfall factored into how fast it moves.

**Monte Carlo uncertainty.** A single projected line quietly implies the forecast wind is exact. Toggle this on and the same spread maths runs 32 times per fire with the wind perturbed inside its real forecast error, and the region those scenarios land in is drawn underneath. A tight shape means the projection is trustworthy. A wide one means it genuinely could go several ways.

**"Am I in danger?"** — type any address and get a Safe / Watch / Danger verdict based on nearby fires *and their predicted spread*, with the nearest real threat's distance, confidence, and estimated time of arrival. With Monte Carlo on it reports how *often* a fire reaches you across sampled scenarios, and raises its own rating if enough of them come out worse than the single best guess.

**Smoke forecasting.** Circle any region and it finds every fire inside, pulls the *measured* PM2.5 at the strongest ones, and projects where that smoke goes over the next 24 hours on forecast wind — drawn as a plume that is dark and opaque where the air is genuinely hazardous and fades to nothing where it has diluted back to background.

**A 7-day playback.** One day of detections is a scatter of dots. The same region played day by day shows a fire front actually moving across the ground.

Plus: click any fire for its detail, a prediction playback animation, a "biggest fires right now" leaderboard, and one-tap image export that saves to your camera roll on mobile.

---

## How we built it

- **Backend:** FastAPI (Python), holding the entire world's fire dataset — roughly 455,000 detections — in memory and refreshing it on a timer, so every request is a local operation rather than a live call to NASA.
- **Frontend:** a single self-contained HTML file. Three.js and globe.gl for the globe; all prediction maths, all rendering, no build step and no framework.
- **Data:** NASA FIRMS (VIIRS satellite), Natural Resources Canada CWFIS (ground-confirmed), Open-Meteo (wind, terrain, air quality), OpenStreetMap/Nominatim (geocoding).
- **Hosting:** Render free tier — a 512 MB memory ceiling that shaped a genuinely large amount of the engineering.

Every fire marker on screen is a single batched draw call rather than thousands of individual objects, which is what keeps tens of thousands of them smooth on a phone.

---

## Challenges we ran into

**A rate limit that had nothing to do with us.** Predictions kept failing with `429 Too Many Requests` from our weather provider. We assumed we were calling too often and added backoff, then jitter, then serialised every outbound call — and it still failed. The real cause: our host's free tier shares outbound IP addresses across unrelated customers, and the provider rate-limits by IP. The quota being exhausted was largely *someone else's traffic*, and no amount of tuning our own pacing could fix it. We stopped trying to prevent it and built graceful degradation instead, so a prediction always returns something.

**Half of Siberia didn't exist.** Point-in-polygon maths treats longitude as a flat number line. Russia's outline crosses the antimeridian, jumping from +180° to −180°, which tears the shape in half in flat coordinates. Every fire in Siberia — some of the largest on Earth — tested as being in no country at all, and searching "Russia" returned almost nothing. Silently, for weeks, without a single error. It now returns 98,309 detections. The obvious one-line fix turned out to be *worse than the bug*, resolving New Delhi to Mexico.

**The fires a satellite can't see are the big ones.** Thermal detection from orbit has a blind spot that gets *worse* as a fire gets more serious: a large, established fire makes enough smoke to hide its own hottest core from a sensor looking straight down at it. Our 7-day replay was satellite-only, and playing back Canada showed fires apparently *thinning out* over a week when they were actually intensifying. Watching your own animation quietly under-report a fire season is a good way to find a data problem.

We found that Canada's CWFIS publishes its reported-fires layer as a proper time series — every record carries a validity window — so you can ask what the ground-confirmed picture was on a *past* day, rather than filtering today's list. Merging that in, we measured that on the smokiest days in eastern Canada roughly **three quarters** of the fire activity came from ground reports. The satellite-only replay had been showing about a third of reality.

**Three out-of-memory crashes, all the same mistake.** Caching assembled Python objects instead of compressed response bytes, three times in three different places, on a 512 MB box. The third time we finally wrote it down.

**A 29-second first load.** Profiling found the bottleneck was nowhere near where we'd have guessed — see below.

---

## Accomplishments we're proud of

**We profiled instead of guessing, and it paid off enormously.** The cold first load took 29.4 seconds. Everyone's instinct for "why is clustering 455,000 fires slow" is the clustering — it was 10% of the time. The real costs were a generic serializer doing unnecessary work, and an entire expensive request that existed to display a *single number* while starving the request the user was actually waiting on. Cold load is now **0.39 seconds**, and the globe is interactive in under one.

**We built the tool that says our own model isn't that good.** `validate_predictions.py` measures the spread projection against what actually happened, using a *persistence baseline* — "predict the fire doesn't move" — because fire detections cluster, so any prediction lands near some fire and raw error means nothing.

It found the projection does **not** beat persistence. Digging into why turned up something genuinely interesting: the ground truth is biased. Smoke blows downwind and obscures satellite thermal detection there, so new detections systematically skew *upwind* (36–61° from reported wind, versus 90° for random). That's a detection artifact, not fire behaviour. Properly validating this needs mapped fire perimeters, not point detections. We'd rather ship that honestly than quietly tune a constant until the graph looked good.

**Refusing to invent a number.** The air quality readout has no fallback, unlike everything else in the app. A made-up wind direction only misaims a drawing on a globe. A made-up air quality figure is a health number someone might act on. If we don't have it, we don't show it.

---

## What we learned

- **The Earth is round and your maths probably isn't.** Wrap-around bugs don't throw exceptions; they just quietly return wrong answers.
- **A rate limit isn't always about your traffic.** Shared infrastructure means someone else's usage can be your outage.
- **Retrying is the wrong instinct against a *sustained* failure.** It's correct for a brief burst and actively harmful otherwise — every retry is more load on something already overwhelmed.
- **Ask the API instead of guessing.** Two assumptions about NASA's history endpoint were settled in under a minute by sending it one request each. One was wrong.
- **Check the instrument before optimising against it.** Three separate times, a "slow" measurement turned out to be our measuring tool's overhead, not the server.
- **The fastest work is the work you don't do on the request path.** The biggest win wasn't making anything faster — it was noticing the expensive result didn't depend on the request, and building it on a timer instead.

---

## What's next

- **Accounts and crowd-sourced reporting** (via Supabase) — scoped and deliberately deferred. Community reports need moderation and row-level security to be trustworthy, and doing that badly is worse than not doing it.
- **Proper prediction validation against mapped fire perimeters** (e.g. NIFC), which is the only way to settle whether the spread model is actually any good.
- **Wider ground-confirmed coverage.** Canada has an excellent open incident feed. Most countries don't.
- A detection-age filter, and shareable URLs that encode the current view.

---

## Built with

`python` `fastapi` `three.js` `globe.gl` `webgl` `javascript` `nasa-firms` `open-meteo` `openstreetmap` `render`

---

## Submission checklist

- [ ] Screenshots — the globe with predictions running; a Monte Carlo footprint; the smoke plume; the "Am I in danger?" result
- [ ] Demo video (2–3 min): open globe → search a country → prediction + Monte Carlo → play animation → smoke check → address check
- [ ] Live URL + GitHub link
- [ ] Confirm the live deploy is on the current commit before submitting
