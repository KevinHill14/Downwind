# Downwind: 3 minute demo script

Target runtime 3:00. Timings are generous, so if you talk at a normal pace you will land around 2:50.

Two things worth doing before you record:
- Load the site once first so the data is warm. A cold Render instance takes a few seconds to wake.
- Have Portugal or California already typed once so the search dropdown is quick.

Read this out loud once before recording. Anywhere it feels like a sentence you would not actually say, change it. Your own phrasing beats a script every time.

---

## 0:00 to 0:20 | Hook

> **[Screen: the globe, slowly rotating, no panels open]**

"In 2023 and again in 2026, smoke from Canadian wildfires turned the sky over New York and Southern Ontario orange. Yet the nearest fire was more than a thousand kilometres away.

Every wildfire map I could find would have told those people they were safe. Technically they were right. There was no fire near them. There was just air they could not breathe.

So I made Downwind."

---

## 0:20 to 0:45 | What you are looking at

> **[Slowly spin the globe. Zoom toward Africa or Siberia so the density is obvious.]**

"Every dot is a real fire, burning right now. About a hundred thousand active detections worldwide, pulled from three NASA satellites and refreshed every ten minutes.

Colour is intensity, from small brush fires in yellow up to catastrophic in deep red. And the whole world is already in memory on the server, so panning around is instant. Nothing here is a stock dataset. This is the planet this exact moment."

---

## 0:45 to 1:15 | Prediction and uncertainty

> **[Search "Portugal". Let the camera fly. Predictions draw automatically.]**

"You can search a country and it projects where those fires are heading, using real wind forecast data. You can push that from a single wind reading up to hourly wind, terrain, and weather by using this slider.

**[Toggle "Show uncertainty" on. Point at the cyan.]**

But a single line implies the forecast is exact, and it never is. So this toggle re-runs the whole projection thirty-two times with the wind varied inside its real margin of error. The cyan is everywhere the fire could still reach.

Wider cones mean the wind could genuinely go several directions. Tighter cones mean you can trust it. But I would rather show you an honest blob than a confident line that is wrong."

---

## 1:15 to 2:05 | The Canada problem

> **[This is the Paint section. Switch to your drawings.]**

**Drawing 1: two stick figures on the ground, sun above, clear sky.**

"Quick detour, because this is the part I got wrong for about a week.

Here is you, standing outside, looking up at the sun. Easy. Nothing in the way."

**Drawing 2: same scene, thick clouds slide in between.**

"Now put clouds in between. The sun is still there. It is burning exactly as hard as it was a second ago. You just cannot see it any more.

Now flip that whole picture upside down."

**Drawing 3: same layout, but swap the pieces. The sun becomes a fire on the ground. The stick figures become a satellite up in space, looking down. The clouds become smoke.**

"The fire is the sun. The satellite is you. And the smoke is the cloud.

Satellites find fires by looking for heat from orbit. But a big, well-established fire makes an enormous amount of smoke, and that smoke sits directly between the fire and the satellite.

So the blind spot gets worse exactly as the fire gets more severe. The fires most worth showing you are the ones most likely to be missing."

> **[Back to the app. Canada on screen. Hit "Replay the last 7 days".]**

"I found this by comparing government maps to my map of Canada, and also knowing there were bad wildfires. It was evident something was wrong.

The fix was Canada's ground crews. Natural Resources Canada publishes every agency-reported fire with a validity window, so you can ask what was confirmed burning on a past day.

**[Cut to your two screenshots: satellite-only first, then with the ground data merged in.]**

In conclusion, Canada went from this (Screenshot), to this (Screenshot) and it aligned with government maps too.

---

## 2:05 to 2:40 | Smoke, the actual point

> **[Click "Where's the smoke going?". Click a dense fire region. Let the plume draw.]**

"Which brings me back to the reason I built this.

You can circle any region and it finds every fire inside, reads the measured PM2.5 at the strongest fires, and projects where that smoke goes over the next forty-eight hours using forecast wind.

The plume is dark where the air is genuinely dangerous and fades out where it has diluted back to normal.

**[Type an address into "Am I in danger?" that is well away from any fire but downwind. Hit Check.]**

And this is the question the whole project is built around. Am I safe? You can type an address, and you get the fire verdict and the air quality separately. Because you can be perfectly safe from flame and still be breathing something you should not."

---

## 2:40 to 3:00 | Close

> **[Pull back out to the full globe. Let it rotate.]**

"There is a lot more in here. Seven-day replay anywhere on Earth, a biggest-fires leaderboard, click any fire for detail, export any view as an image, along with many other config options to improve user experience.

But the one thing I would want you to take away is this. Fire maps tell you where it is burning. Downwind tells you where it is going, and who is going to be breathing it."

---

## Delivery notes

- **Slow down on the hook and the close.** Those are the two parts people actually remember. Everything in the middle can move quickly.
- **Do not narrate your clicks.** Saying "now I'm going to click on the smoke tool" wastes a second and sounds like a tutorial. Just click it and keep talking about why it matters.
- **The Paint section is the strongest 50 seconds you have.** It shows you found a real problem in your own work rather than just wiring up an API. Let it breathe, and do not rush the flip from sun to fire, since that is the moment the idea lands.
- **Record the audio in one take if you can.** Small stumbles read as human. Perfectly even narration reads as generated.
- If you run long, the first thing to cut is the leaderboard and export mention at 2:40. The last thing to cut is Canada.
