"""
Validates Fire Tracker's spread prediction against what actually happened.

The prediction math in static/index.html is a hand-tuned heuristic that has
never been measured against reality - its constants are physically motivated
but arbitrary. This script measures it: take fires as they were on some past
day, run the same projection forward using the wind that actually blew, and
compare the predicted positions against where fires really were detected
afterwards.

The number that matters is NOT the raw error distance - it's the comparison
against a persistence baseline ("predict the fire doesn't move at all").
Fire detections cluster, so *any* prediction lands near *some* fire; if the
projection can't beat "it stays put", it isn't adding information, however
good its absolute error looks.

WHAT THIS FOUND (Aug 2026, Iberia / W USA / Australia): the projection does
not beat persistence, and error grows monotonically with projected distance.
That looks damning until you notice the ground truth is biased: smoke blows
downwind and hides thermal detections there, so detections are systematically
MISSING exactly where spread is predicted. Measured directly, new detections
around a fire skew upwind (36-61 deg from the reported wind direction, vs 90
for random) - which is a detection artifact, not fire behaviour, since
Open-Meteo follows the meteorological convention and the app's +180 is right.

So this script is a regression check and a magnitude sanity check. A real
accuracy verdict needs mapped fire perimeters as ground truth, not points.

Usage:
    python validate_predictions.py --bbox -10,36,4,44 --days-ago 4
    python validate_predictions.py --bbox -125,32,-114,42 --days-ago 5 --hours 48

Needs FIRMS_MAP_KEY (same key the server uses; read from .env).
"""
import argparse
import asyncio
import math
import os
import statistics
from datetime import date, datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

FIRMS_MAP_KEY = os.environ.get("FIRMS_MAP_KEY", "")
FIRMS_SOURCES = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"]
FIRMS_DATED_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{area}/1/{day}"
WIND_URL = "https://api.open-meteo.com/v1/forecast"

# Kept in sync with static/index.html - this is the math under test.
LARGE_FIRE_MIN_FRP = 27.4
FORECAST_STEP_HOURS = 6


def spread_distance_km(wind_speed_kmh: float, frp: float, hours: float) -> float:
    intensity = min(2.0, 0.5 + math.log1p(frp) / 5)
    return (0.15 * wind_speed_kmh + 0.3) * intensity * (hours / 6)


def destination_point(lat, lng, bearing_deg, distance_km):
    R = 6371.0
    b = math.radians(bearing_deg)
    p1 = math.radians(lat)
    l1 = math.radians(lng)
    dr = distance_km / R
    p2 = math.asin(math.sin(p1) * math.cos(dr) + math.cos(p1) * math.sin(dr) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(dr) * math.cos(p1),
                         math.cos(dr) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def cluster_fires(fires, grid_deg):
    """Same aggregation the app predicts on - it projects clusters, not raw
    detections, so validating raw detections would be testing something the
    app never actually does."""
    large = [f for f in fires if f["frp"] >= LARGE_FIRE_MIN_FRP]
    small = [f for f in fires if f["frp"] < LARGE_FIRE_MIN_FRP]
    clusters = [{"lat": f["lat"], "lng": f["lng"], "totalFrp": f["frp"]} for f in large]
    groups = {}
    for f in small:
        key = (round(f["lat"] / grid_deg), round(f["lng"] / grid_deg))
        groups.setdefault(key, []).append(f)
    for group in groups.values():
        total = sum(f["frp"] for f in group)
        w = total or len(group)
        clusters.append({
            "lat": sum(f["lat"] * (f["frp"] or 1) for f in group) / w,
            "lng": sum(f["lng"] * (f["frp"] or 1) for f in group) / w,
            "totalFrp": total,
        })
    return clusters


async def fetch_firms_day(client, area, day: date):
    """All detections for a single UTC day, across every VIIRS satellite."""
    out = []
    for source in FIRMS_SOURCES:
        url = FIRMS_DATED_URL.format(key=FIRMS_MAP_KEY, source=source, area=area, day=day.isoformat())
        try:
            resp = await client.get(url, timeout=60)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            print(f"  ! {source} {day}: {type(e).__name__}")
            continue
        lines = resp.text.strip().splitlines()
        if len(lines) < 2:
            continue
        idx = {name: i for i, name in enumerate(lines[0].split(","))}
        if "latitude" not in idx:
            print(f"  ! {source} {day}: unexpected response: {lines[0][:90]}")
            continue
        for line in lines[1:]:
            c = line.split(",")
            try:
                out.append({
                    "lat": float(c[idx["latitude"]]),
                    "lng": float(c[idx["longitude"]]),
                    "frp": float(c[idx["frp"]]),
                })
            except (ValueError, IndexError, KeyError):
                continue
    return out


async def fetch_past_wind(client, points, start_dt: datetime, hours: int):
    """Hourly wind that ACTUALLY blew over the validation window.

    Uses the forecast endpoint's past_days rather than the ERA5 archive:
    the archive lags several days, which would rule out validating against
    recent fires. Chunked and serialized for the same reason the server
    does it - Open-Meteo rate-limits by IP.
    """
    past_days = min(92, max(1, (datetime.now(timezone.utc).date() - start_dt.date()).days + 1))
    results = {}
    CHUNK = 40
    for i in range(0, len(points), CHUNK):
        chunk = points[i:i + CHUNK]
        params = {
            "latitude": ",".join(str(round(p["lat"], 4)) for p in chunk),
            "longitude": ",".join(str(round(p["lng"], 4)) for p in chunk),
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "kmh",
            "past_days": past_days,
            "forecast_days": 2,
        }
        try:
            resp = await client.get(WIND_URL, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            print(f"  ! wind chunk {i//CHUNK}: {type(e).__name__}: {e}")
            continue
        if isinstance(data, dict):
            data = [data]
        for point, entry in zip(chunk, data):
            hourly = entry.get("hourly") or {}
            times = hourly.get("time") or []
            speeds = hourly.get("wind_speed_10m") or []
            dirs = hourly.get("wind_direction_10m") or []
            steps = []
            for h in range(0, hours + 1, FORECAST_STEP_HOURS):
                stamp = (start_dt + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00")
                if stamp not in times:
                    continue
                j = times.index(stamp)
                if j >= len(speeds) or speeds[j] is None or dirs[j] is None:
                    continue
                steps.append({"h": h, "speed": speeds[j],
                              "toward": (dirs[j] + 180) % 360,  # assuming "from" convention
                              "raw": dirs[j]})
            if steps:
                results[(point["lat"], point["lng"])] = steps
        await asyncio.sleep(1.0)  # gentle: never burst at Open-Meteo
    return results


def project(cluster, steps, hours, scale=1.0, bearing_override=None):
    """The app's strength-2 walk: step the position every 6h along the wind
    at that moment.

    `scale` multiplies every step's distance, so the whole spread-rate
    constant can be swept without re-fetching anything. `bearing_override`
    replaces the wind direction entirely - used as a control to test
    whether the wind direction carries any real signal, or whether the
    projection is only ever as good as its step SIZE.
    """
    lat, lng = cluster["lat"], cluster["lng"]
    for i, step in enumerate(steps):
        nxt = steps[i + 1]["h"] if i + 1 < len(steps) else hours
        span = min(nxt, hours) - step["h"]
        if span <= 0:
            continue
        bearing = step["toward"] if bearing_override is None else bearing_override
        lat, lng = destination_point(lat, lng, bearing,
                                     spread_distance_km(step["speed"], cluster["totalFrp"], span) * scale)
    return lat, lng


def nearest_km(lat, lng, fires):
    return min((haversine_km(lat, lng, f["lat"], f["lng"]) for f in fires), default=None)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", required=True, help="west,south,east,north")
    ap.add_argument("--days-ago", type=int, default=4, help="how far back the starting day is")
    ap.add_argument("--hours", type=int, default=24, help="projection window to validate")
    ap.add_argument("--max-clusters", type=int, default=60)
    ap.add_argument("--grid", type=float, default=0.2)
    args = ap.parse_args()

    if not FIRMS_MAP_KEY:
        print("FIRMS_MAP_KEY not set (put it in .env)")
        return

    start_day = date.today() - timedelta(days=args.days_ago)
    end_day = start_day + timedelta(hours=args.hours) if args.hours >= 24 else start_day + timedelta(days=1)
    end_day = (datetime.combine(start_day, datetime.min.time()) + timedelta(hours=args.hours)).date()
    start_dt = datetime.combine(start_day, datetime.min.time()).replace(tzinfo=timezone.utc)

    print(f"Region {args.bbox} | start {start_day} | +{args.hours}h -> {end_day}")

    async with httpx.AsyncClient() as client:
        print("\nFetching starting fires...")
        start_fires = await fetch_firms_day(client, args.bbox, start_day)
        print(f"  {len(start_fires)} detections on {start_day}")

        print("Fetching what actually happened...")
        actual_days = sorted({start_day + timedelta(days=d) for d in range(1, max(2, args.hours // 24 + 1))})
        actual_fires = []
        for d in actual_days:
            got = await fetch_firms_day(client, args.bbox, d)
            print(f"  {len(got)} detections on {d}")
            actual_fires.extend(got)

        if not start_fires or not actual_fires:
            print("\nNot enough data to validate (try a bigger region, or a different date).")
            return

        clusters = sorted(cluster_fires(start_fires, args.grid), key=lambda c: -c["totalFrp"])[:args.max_clusters]
        print(f"\n{len(clusters)} clusters to project (of {len(start_fires)} detections)")

        print("Fetching the wind that actually blew...")
        winds = await fetch_past_wind(client, clusters, start_dt, args.hours)
        print(f"  wind for {len(winds)}/{len(clusters)} clusters")

    pred_errors, base_errors, wins, moved = [], [], 0, []
    for c in clusters:
        steps = winds.get((c["lat"], c["lng"]))
        if not steps:
            continue
        plat, plng = project(c, steps, args.hours)
        pe = nearest_km(plat, plng, actual_fires)
        be = nearest_km(c["lat"], c["lng"], actual_fires)
        if pe is None or be is None:
            continue
        pred_errors.append(pe)
        base_errors.append(be)
        moved.append(haversine_km(c["lat"], c["lng"], plat, plng))
        if pe < be:
            wins += 1

    if not pred_errors:
        print("\nNo clusters could be scored.")
        return

    n = len(pred_errors)
    print("\n" + "=" * 62)
    print(f"VALIDATION - {n} clusters, {args.hours}h projection")
    print("=" * 62)
    print(f"  Predicted position -> nearest real fire   median {statistics.median(pred_errors):6.2f} km"
          f"   mean {statistics.mean(pred_errors):6.2f} km")
    print(f"  Persistence (no movement) baseline        median {statistics.median(base_errors):6.2f} km"
          f"   mean {statistics.mean(base_errors):6.2f} km")
    print(f"  Median projected travel distance          {statistics.median(moved):6.2f} km")
    print()
    print(f"  Prediction beat persistence in {wins}/{n} cases ({100 * wins / n:.0f}%)")
    delta = statistics.median(base_errors) - statistics.median(pred_errors)
    verdict = "adds information" if delta > 0 and wins / n > 0.5 else "NOT better than assuming no movement"
    print(f"  Median improvement over persistence: {delta:+.2f} km  ->  {verdict}")
    print("=" * 62)

    # --- Tuning sweep -----------------------------------------------------
    # Re-projects the SAME fetched wind at different spread rates, so the
    # whole curve costs no extra API calls. This separates the two ways the
    # projection can be wrong: moving the wrong DISTANCE (a constant this
    # sweep can fix) versus moving in the wrong DIRECTION (which it can't -
    # if the random-bearing control below scores as well as real wind, the
    # wind signal isn't doing any work and no constant will save it).
    scored = [(c, winds[(c["lat"], c["lng"])]) for c in clusters if winds.get((c["lat"], c["lng"]))]

    def evaluate(scale, bearing_override=None):
        errs, w = [], 0
        for c, steps in scored:
            plat, plng = project(c, steps, args.hours, scale, bearing_override)
            pe = nearest_km(plat, plng, actual_fires)
            be = nearest_km(c["lat"], c["lng"], actual_fires)
            if pe is None or be is None:
                continue
            errs.append(pe)
            if pe < be:
                w += 1
        return (statistics.median(errs), statistics.mean(errs), w / len(errs)) if errs else (None, None, 0)

    print("\nTUNING SWEEP - spread-rate multiplier vs accuracy")
    print(f"{'scale':>7} {'median km':>11} {'mean km':>9} {'beats persistence':>19}")
    best = None
    for scale in [0.0, 0.15, 0.25, 0.4, 0.6, 0.8, 1.0, 1.3, 1.7]:
        med, mean, winrate = evaluate(scale)
        if med is None:
            continue
        print(f"{scale:>7.2f} {med:>11.2f} {mean:>9.2f} {winrate * 100:>18.0f}%")
        if best is None or med < best[1]:
            best = (scale, med, winrate)

    if best:
        print(f"\n  Best spread-rate multiplier: {best[0]:.2f}  (median {best[1]:.2f} km, beats persistence {best[2]*100:.0f}%)")

    # Direction control has to run at a NON-ZERO scale - with zero travel
    # the bearing is irrelevant and the control would trivially tie.
    real_med = evaluate(1.0)[0]
    ctrl_med = statistics.mean([evaluate(1.0, b)[0] for b in (0, 90, 180, 270)])
    print(f"\n  Direction check at scale 1.0 - real wind {real_med:.2f} km vs arbitrary bearing {ctrl_med:.2f} km")
    print("  ->", "real wind is better - direction carries signal" if real_med < ctrl_med
          else "real wind is NOT better - direction is carrying no signal here")

    # --- Does the fire actually GROW downwind? ---------------------------
    # The test above asks "did the fire MOVE to where we said". But a
    # wildfire mostly doesn't translate - it keeps burning where it is and
    # expands, which is why persistence is so hard to beat. So this asks the
    # question the app actually cares about: of the new detections that
    # appeared around this fire, are they biased in the direction the wind
    # was blowing? Random would average 90 degrees of error.
    # Measured over an ANNULUS, not a disc: detections sitting on top of the
    # original fire are the persistent core, and including them lets the core
    # dominate the centroid and wash out the growth direction entirely.
    GROWTH_INNER_KM, GROWTH_OUTER_KM = 3.0, 25.0

    def angle_between(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)

    toward_angles, raw_angles, control_angles = [], [], []
    rng = __import__("random")
    rng.seed(1234)  # reproducible control
    for c, steps in scored:
        ring = [f for f in actual_fires
                if GROWTH_INNER_KM <= haversine_km(c["lat"], c["lng"], f["lat"], f["lng"]) <= GROWTH_OUTER_KM]
        if len(ring) < 4:
            continue
        w = sum(f["frp"] or 1 for f in ring)
        clat = sum(f["lat"] * (f["frp"] or 1) for f in ring) / w
        clng = sum(f["lng"] * (f["frp"] or 1) for f in ring) / w
        y = math.sin(math.radians(clng - c["lng"])) * math.cos(math.radians(clat))
        x = (math.cos(math.radians(c["lat"])) * math.sin(math.radians(clat))
             - math.sin(math.radians(c["lat"])) * math.cos(math.radians(clat)) * math.cos(math.radians(clng - c["lng"])))
        growth_bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
        toward_angles.append(angle_between(growth_bearing, steps[0]["toward"]))
        raw_angles.append(angle_between(growth_bearing, steps[0]["raw"]))
        control_angles.append(angle_between(growth_bearing, rng.uniform(0, 360)))

    if toward_angles:
        mt, mr, mc = (statistics.mean(toward_angles), statistics.mean(raw_angles), statistics.mean(control_angles))
        print(f"\n  Growth direction ({len(toward_angles)} fires with growth in the {GROWTH_INNER_KM:.0f}-{GROWTH_OUTER_KM:.0f} km ring)")
        print(f"    vs wind_direction + 180 (what the app uses): {mt:>5.0f} deg")
        print(f"    vs wind_direction as reported:               {mr:>5.0f} deg")
        print(f"    vs a random bearing (control, should be ~90):{mc:>5.0f} deg")
        if mr < mt - 10:
            print("  -> new detections appear UPWIND of each fire.")
            print("     This is NOT evidence the app's wind convention is flipped - Open-Meteo")
            print("     follows the meteorological standard (direction wind comes FROM), so the")
            print("     +180 is correct. The likely cause is detection bias: a fire's smoke plume")
            print("     blows downwind and obscures satellite thermal detection there, so downwind")
            print("     detections go MISSING and the surviving ones sit upwind. Which means this")
            print("     metric is biased against any downwind projection - see the caveat below.")
        elif mt < mr - 10:
            print("  -> growth appears downwind, consistent with the app's wind convention")
        else:
            print("  -> neither convention clearly predicts growth in this sample")

    print("\n" + "-" * 62)
    print("READ THE NUMBERS ABOVE WITH THESE CAVEATS:")
    print("-" * 62)
    print("1. Fire detections are clustered, so landing near SOME fire is easy.")
    print("   Only the persistence comparison says anything about movement.")
    print("2. A wildfire mostly does NOT translate - it keeps burning where it")
    print("   is and expands. Persistence is therefore a genuinely strong")
    print("   baseline, and losing to it does not by itself mean the spread")
    print("   direction is wrong.")
    print("3. Smoke obscures satellite detection downwind of a fire, so the")
    print("   ground truth itself is thinnest exactly where this model predicts")
    print("   spread. That biases 'distance to nearest detection' AGAINST any")
    print("   downwind projection, and is why the spread-rate sweep bottoms out")
    print("   at 0 - do not read that as 'the fire does not move'.")
    print()
    print("Validating this properly needs mapped fire PERIMETERS (e.g. NIFC) as")
    print("ground truth rather than point detections. Treat this script as a")
    print("regression check on the math and a sanity check on magnitudes, not")
    print("as a verdict on real-world accuracy.")


if __name__ == "__main__":
    asyncio.run(main())
