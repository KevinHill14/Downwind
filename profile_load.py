"""Times each stage of a cold /api/fires world request, so optimisation
targets whatever is actually slow rather than whatever looks slow.

Run:  python profile_load.py
"""
import asyncio
import gzip
import json
import time

from fastapi.encoders import jsonable_encoder

import main


def stage(label, fn):
    t0 = time.perf_counter()
    out = fn()
    ms = (time.perf_counter() - t0) * 1000
    print(f"  {label:<42} {ms:>9.0f} ms")
    return out, ms


async def run():
    main._http_client = __import__("httpx").AsyncClient()
    print("fetching the world dataset (not part of the request path)...")
    t0 = time.perf_counter()
    await main._refresh_world_fires()
    print(f"  fetch+parse: {time.perf_counter() - t0:.1f}s, {len(main._world_fires):,} raw detections\n")

    # The exact parameters the frontend's first load sends.
    for label, grid, min_frp, min_conf in [
        ("DISPLAY  (grid=0.5, small fires off)", 0.5, 3, "h"),
        ("ESTIMATE (grid=0.05, small fires off)", 0.05, 3, "h"),
    ]:
        print(f"{label}")
        fires = main._world_fires
        fires, _ = stage("filter min_frp", lambda: [f for f in fires if f["frp"] >= min_frp])
        clustered, _ = stage("cluster", lambda: main._cluster_fires(fires, grid))
        result = {"fires": clustered, "count": len(clustered), "raw_count": len(fires)}
        encoded, _ = stage("jsonable_encoder", lambda: jsonable_encoder(result))
        body, _ = stage("json.dumps", lambda: json.dumps(encoded).encode("utf-8"))
        stage("gzip compresslevel=6", lambda: gzip.compress(body, compresslevel=6))
        stage("gzip compresslevel=1", lambda: gzip.compress(body, compresslevel=1))
        # What it would cost WITHOUT jsonable_encoder, since these are already
        # plain dicts of primitives once clustered.
        stage("json.dumps direct (no encoder)", lambda: json.dumps(result).encode("utf-8"))
        print(f"  -> {len(clustered):,} clusters, {len(body)/1024/1024:.2f} MB json\n")

    await main._http_client.aclose()


asyncio.run(run())
