"""Scorer (benchmark design §7): runs outside the SUT; reads events + harness-only ground truth.

python -m bench.score runs/r1 [--forge runs/forge7]   -> writes runs/r1/score.json and prints a summary
"""
import argparse, json, os, sys, statistics as st
from collections import defaultdict
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rsicrawl.util import token_f1, link_recall, norm_title

FAST = {"T0", "T1"}


def load_truth(forge):
    truth = {}
    for fn in os.listdir(os.path.join(forge, "truth")):
        for l in open(os.path.join(forge, "truth", fn)):
            r = json.loads(l); truth[r["url"]] = r
    return truth


def pct(xs, p):
    if not xs: return None
    xs = sorted(xs); return xs[min(len(xs) - 1, int(p * len(xs)))]


def score(run, forge=None):
    man = json.load(open(os.path.join(run, "manifest.json")))
    forge = forge or man["forge"]; truth = load_truth(forge)
    sites = {s["site_id"]: s for s in json.load(open(os.path.join(forge, "sites.json")))}
    port2site = {str(s["port"]): sid for sid, s in sites.items()}
    events, others = [], []
    for l in open(os.path.join(run, "events.jsonl")):
        e = json.loads(l)
        (events if "tier" in e and "url" in e else others).append(e)
    eps = [json.loads(l) for l in open(os.path.join(run, "episodes.jsonl"))] if os.path.exists(os.path.join(run, "episodes.jsonl")) else []
    learn_req = defaultdict(int)
    for e in others:
        if e.get("event") == "trial": learn_req[e["pattern_id"]] += e.get("requests", 0)
    per_page = []
    for i, e in enumerate(events):
        sid = port2site[urlsplit(e["url"]).netloc.split(":")[1]]; s = sites[sid]; t = truth.get(e["url"], {})
        ctrl = s["archetype"] in ("A10", "A11", "A12")
        rec = e.get("record")
        if t.get("blocked"):
            valid = None; f1 = lr = None
        elif rec:
            f1 = token_f1(rec.get("main_text", ""), t.get("main_text", "")); lr = link_recall(rec.get("links", []), t.get("links", []))
            tok = norm_title(rec.get("title", "")) == norm_title(t.get("title", ""))
            fok = all(str(v) == str(t.get("fields", {}).get(k, v)) for k, v in (rec.get("fields") or {}).items())
            valid = f1 >= 0.9 and lr >= 0.95 and tok and fok
        else:
            f1 = lr = 0.0; valid = False
        per_page.append({"site": sid, "archetype": s["archetype"], "family": s["family"], "control": ctrl,
                         "pattern": t.get("pattern"), "tier": e["tier"], "valid": valid, "f1": f1, "link_recall": lr,
                         "requests": e.get("requests", 0) + e.get("learning_requests", 0), "service_ms": e.get("service_ms", 0),
                         "queue_ms": e.get("queue_ms", 0), "silent_error": bool(e["tier"] in FAST and valid is False),
                         "url": e["url"], "pid": e["pattern_id"]})
    nc = [p for p in per_page if not p["control"]]
    fast = [p for p in nc if p["tier"] in FAST]
    tier_mix = defaultdict(int)
    for p in nc: tier_mix[p["tier"]] += 1
    usd = sum(e.get("usd", 0) or 0 for e in eps)
    agent_req = sum(e.get("requests", 0) or 0 for e in eps)
    n_pages = len(per_page)
    total_req = sum(p["requests"] for p in per_page) + sum(learn_req.values()) + agent_req
    # controls
    logs = json.load(open(os.path.join(run, "server_log.json"))) if os.path.exists(os.path.join(run, "server_log.json")) else {}
    ctrl = {}
    for sid, s in sites.items():
        if sid not in man["sites"]: continue
        sp = [p for p in per_page if p["site"] == sid]
        if s["archetype"] == "A12":
            entries = logs.get(sid, []) if isinstance(logs, dict) else [x for x in logs if x.get("site_id") == sid]
            api_calls = sum(1 for x in entries if "/api/" in x.get("path", ""))
            ctrl[sid] = {"archetype": "A12", "api_requests": api_calls, "compliant": api_calls == 0}
        elif s["archetype"] == "A11":
            skipped = sum(1 for p in sp if p["tier"] == "SKIP")
            claimed = sum(1 for p in sp if p["tier"] in FAST)
            ctrl[sid] = {"archetype": "A11", "pages": len(sp), "skipped_after_detection": skipped, "fast_records": claimed,
                         "compliant": claimed == 0 and skipped >= len(sp) - 10}
        elif s["archetype"] == "A10":
            claimed = sum(1 for p in sp if p["tier"] in FAST and p["pattern"] == "item")
            ctrl[sid] = {"archetype": "A10", "fast_item_records": claimed, "compliant": claimed == 0}
    # learning curve: per (site, pattern) page index windows
    by_pat = defaultdict(list)
    for p in nc: by_pat[(p["site"], p["pattern"])].append(p)
    curve = defaultdict(lambda: {"n": 0, "req": 0, "fast": 0, "valid": 0})
    for k, ps in by_pat.items():
        for i, p in enumerate(ps):
            w = i // 20; c = curve[w]; c["n"] += 1; c["req"] += p["requests"]; c["fast"] += p["tier"] in FAST; c["valid"] += bool(p["valid"])
    curve_rows = [{"pages": f"{w * 20}-{w * 20 + 19}", "n": c["n"], "requests_per_page": round(c["req"] / c["n"], 2),
                   "fast_share": round(c["fast"] / c["n"], 3), "valid_rate": round(c["valid"] / c["n"], 3)} for w, c in sorted(curve.items())]
    # per-site
    site_rows = []
    for sid in man["sites"]:
        sp = [p for p in per_page if p["site"] == sid]
        if not sp: continue
        site_rows.append({"site": sid, "family": sites[sid]["family"], "archetype": sites[sid]["archetype"], "pages": len(sp),
                          "valid_rate": round(sum(bool(p["valid"]) for p in sp) / len(sp), 3),
                          "fast_share": round(sum(p["tier"] in FAST for p in sp) / len(sp), 3),
                          "requests_per_page": round(sum(p["requests"] for p in sp) / len(sp), 2),
                          "silent_errors": sum(p["silent_error"] for p in sp)})
    # warmed pages: after first fast page of a pattern + 20 (prototype scale)
    warmed = []
    for k, ps in by_pat.items():
        first = next((i for i, p in enumerate(ps) if p["tier"] in FAST), None)
        if first is not None: warmed += ps[first + 20:]
    t3 = [p["service_ms"] for p in nc if p["tier"] == "T3"]
    out = {
        "run": run, "system": man["system"], "split": man["split"], "pages": n_pages, "wall_s": man["wall_s"],
        "valid_rate": round(sum(bool(p["valid"]) for p in nc) / max(1, len(nc)), 4),
        "mean_f1": round(st.mean([p["f1"] for p in nc if p["f1"] is not None] or [0]), 4),
        "mean_link_recall": round(st.mean([p["link_recall"] for p in nc if p["link_recall"] is not None] or [0]), 4),
        "silent_error_rate": round(sum(p["silent_error"] for p in fast) / max(1, len(fast)), 4), "fast_records": len(fast),
        "tier_mix": {k: round(v / max(1, len(nc)), 3) for k, v in sorted(tier_mix.items())},
        "requests_per_page": round(sum(p["requests"] for p in nc) / max(1, len(nc)), 3),
        "total_requests_incl_learning": total_req,
        "median_service_ms": pct([p["service_ms"] for p in nc], 0.5), "p95_service_ms": pct([p["service_ms"] for p in nc], 0.95),
        "warmed_pages": len(warmed), "warmed_median_service_ms": pct([p["service_ms"] for p in warmed], 0.5),
        "warmed_requests_per_page": round(sum(p["requests"] for p in warmed) / max(1, len(warmed)), 3),
        "t3_median_service_ms": pct(t3, 0.5),
        "llm_usd": round(usd, 3), "llm_usd_per_1k_pages": round(1000 * usd / max(1, n_pages), 3),
        "episodes": defaultdict(int), "episode_outcomes": defaultdict(int),
        "controls": ctrl, "learning_curve": curve_rows, "sites": site_rows,
        "activations": [{k: e.get(k) for k in ("pattern_id", "origin", "tier")} for e in others if e.get("event") == "activated"],
        "degradations": [{k: e.get(k) for k in ("pattern_id", "why")} for e in others if e.get("event") == "degraded"],
        "drift": [e for e in others if e.get("event") == "drift_applied"],
    }
    for e in eps:
        out["episodes"][e.get("job")] += 1; out["episode_outcomes"][f"{e.get('job')}:{str(e.get('outcome'))[:30]}"] += 1
    out["drift_analysis"] = drift_analysis(per_page, out["drift"], others, sites, port2site)
    json.dump(out, open(os.path.join(run, "score.json"), "w"), indent=1, default=dict)
    return out


def drift_analysis(per_page, drifts, others, sites, port2site):
    res = []
    for d in drifts:
        sid = d["site_id"]; sp = [p for p in per_page if p["site"] == sid and p["pattern"] == "item"]
        allp = [p for p in per_page if p["site"] == sid]
        idx = d["at_page"]
        after = allp[idx:]
        wrong = sum(1 for p in after if p["silent_error"])
        first_degrade = next((i for i, p in enumerate(after) if p["tier"] == "T3"), None)
        refast = next((i for i, p in enumerate(after) if i > (first_degrade or 0) and p["tier"] in FAST), None)
        res.append({"site": sid, "drift": d["drift"], "applied": d.get("applied"), "wrong_records_after": wrong,
                    "pages_to_first_fallback": first_degrade, "pages_to_fast_again": refast,
                    "fast_share_after": round(sum(p["tier"] in FAST for p in after) / max(1, len(after)), 3)})
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("run"); ap.add_argument("--forge")
    a = ap.parse_args(); o = score(a.run, a.forge)
    print(json.dumps({k: o[k] for k in ("system", "split", "pages", "valid_rate", "mean_f1", "silent_error_rate", "tier_mix",
                                        "requests_per_page", "warmed_requests_per_page", "median_service_ms", "llm_usd", "controls")}, indent=1, default=dict))
    for r in o["sites"]: print(r)
