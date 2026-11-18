"""Build an offline gym (benchmark design §4/§5.3): per pattern of the given split's sites, render M pages with
Lightpanda and store captures + reference records. Used for L3 skill evaluation (train) and for measuring the
analyzer's autonomous solve rate (val/test).

python -m bench.gym --forge runs/forge7 --split train --out runs/gym_train.json [--m 6]
"""
import argparse, asyncio, json, os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rsicrawl.cdp import LightpandaServer, render
from rsicrawl.isolate import build_profile, reference_record
from rsicrawl.util import url_pattern, url_vars, Robots
from rsicrawl.engine import markers_of, CHALLENGE
from bench.run import start_forge, site_lists
import httpx


async def build(forge, splits, m):
    lists, meta = site_lists(forge, splits)
    srv = LightpandaServer(); robots = Robots(); gym = {}
    try:
        async with httpx.AsyncClient() as c:
            for sid, urls in lists.items():
                by = defaultdict(list)
                for u in urls: by[url_pattern(u)].append(u)
                await robots.load(urls[0].split("/", 3)[0] + "//" + urls[0].split("/")[2], c)
                for pid, us in by.items():
                    caps = []
                    for u in us[:m]:
                        cap = await render(srv.ws, u, block=lambda x: not robots.allowed(x), idle_ms=250)
                        if cap.get("status") in (403, 429, 503) and CHALLENGE.search(cap.get("final_html", "")): break
                        caps.append((u, cap))
                    if len(caps) < 4: continue
                    prof = build_profile([(u, c_["final_html"]) for u, c_ in caps])
                    gym[pid] = {"site": sid, "archetype": meta[sid]["archetype"], "family": meta[sid]["family"],
                                "markers": markers_of(caps[0][1]), "profile": prof,
                                "samples": [{"url": u, "vars": url_vars(u), "ref": reference_record(u, c_["final_html"], prof),
                                             "capture": {"url": u, "final_html": c_["final_html"], "requests": c_["requests"]}} for u, c_ in caps]}
                    print(sid, pid, len(caps), flush=True)
    finally:
        srv.close()
    return gym


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--forge", required=True); ap.add_argument("--split", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--m", type=int, default=6); ap.add_argument("--subres", type=int, default=0)
    a = ap.parse_args()
    p = start_forge(a.forge, subres=a.subres)
    try:
        g = asyncio.run(build(a.forge, a.split.split(","), a.m))
    finally:
        p.terminate(); p.wait()
    json.dump(g, open(a.out, "w")); print("patterns:", len(g))
