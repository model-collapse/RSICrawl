"""Speed criteria exactly as defined in the benchmark design (§8 "Speed (C1)", §2a stream rules).

  warmed pages  = pages of a pattern after its first active (fast-tier) page + WARM_OFFSET (50)
  latency       = median service_ms over warmed pages of non-control sites, vs median service_ms of the baseline
  throughput    = pages per host-hour over each site's warmed window (includes queueing for the per-host budget),
                  vs the same site's pages per host-hour in the baseline run; reported as the median over sites
  ceiling       = baseline requests/page / 1 request: the best ratio any system could reach under the request budget

python -m bench.speed RUN BASELINE_RUN [--offset 50]
"""
import argparse, json, os, statistics as st, sys
from collections import defaultdict

FAST = {"T0", "T1"}
CONTROLS = {"A10", "A11", "A12"}


def _load(run, forge):
    sites = {s["site_id"]: s for s in json.load(open(os.path.join(forge, "sites.json")))}
    port2 = {str(s["port"]): k for k, s in sites.items()}
    by = defaultdict(list)
    for l in open(os.path.join(run, "events.jsonl")):
        e = json.loads(l)
        if "tier" not in e or "url" not in e: continue
        sid = port2[e["url"].split(":")[2].split("/")[0]]
        if sites[sid]["archetype"] in CONTROLS: continue
        by[sid].append(e)
    return by


def _per_host(events, offset):
    """Return (warmed events, pages/host-hour over the warmed window)."""
    events = sorted(events, key=lambda e: e["t"])
    if offset is None:
        w = events
    else:
        pats = defaultdict(list)
        for e in events: pats[e["pattern_id"]].append(e)
        w = []
        for ps in pats.values():
            f = next((i for i, p in enumerate(ps) if p["tier"] in FAST), None)
            if f is not None: w += ps[f + offset:]
        w.sort(key=lambda e: e["t"])
    if len(w) < 10: return w, None
    span = w[-1]["t"] - w[0]["t"]
    return w, ((len(w) - 1) / span * 3600 if span > 0 else None)


def speed(run, base, offset=50):
    forge = json.load(open(os.path.join(run, "manifest.json")))["forge"]
    R, B = _load(run, forge), _load(base, forge)
    lat_r, lat_b, ratios, base_req = [], [], {}, []
    for sid in R:
        w, thr = _per_host(R[sid], offset)
        wb, thr_b = _per_host(B.get(sid, []), None)
        lat_r += [e["service_ms"] for e in w]; lat_b += [e["service_ms"] for e in wb]
        base_req += [e.get("requests", 0) for e in wb]
        if thr and thr_b: ratios[sid] = round(thr / thr_b, 2)
    ml_r, ml_b = st.median(lat_r) if lat_r else None, st.median(lat_b) if lat_b else None
    return {"run": run, "baseline": base, "warm_offset": offset, "warmed_pages": len(lat_r),
            "median_latency_ms": ml_r, "baseline_median_latency_ms": ml_b,
            "latency_ratio": round(ml_b / ml_r, 2) if ml_r else None,
            "throughput_ratio_median": round(st.median(ratios.values()), 2) if ratios else None,
            "throughput_ratio_per_site": dict(sorted(ratios.items())),
            "baseline_requests_per_page": round(st.mean(base_req), 2) if base_req else None,
            "throughput_ceiling": round(st.mean(base_req), 2) if base_req else None,
            "pass_latency_10x": bool(ml_r and ml_b / ml_r >= 10),
            "pass_throughput_5x": bool(ratios and st.median(ratios.values()) >= 5)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("run"); ap.add_argument("baseline"); ap.add_argument("--offset", type=int, default=50)
    a = ap.parse_args()
    r = speed(a.run, a.baseline, a.offset)
    json.dump(r, open(os.path.join(a.run, "speed.json"), "w"), indent=1)
    print(json.dumps(r, indent=1))
