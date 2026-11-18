"""Offline autonomous-solve-rate of the analyzer (built-ins + a memory's skills/playbooks) on a gym.

python -m bench.gymscore runs/gym_val.json [--memory DIR] [--playbooks]
"""
import argparse, asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rsicrawl import analyzer as an
from rsicrawl.memory import Memory


async def score(gym, mem, use_playbooks):
    per = {}
    for pid, g in gym.items():
        tr, ho = g["samples"][:3], g["samples"][3:]
        best, rep = await an.analyze(tr, g["profile"], playbooks=mem.playbooks() if (mem and use_playbooks) else (),
                                     skills=mem.skills() if mem else ())
        ok = False
        if best:
            n, _ = await an.verify_offline(best, ho); ok = n == len(ho)
        ctrl = g["archetype"] in ("A10", "A11", "A12")
        per[pid] = {"solved": ok, "origin": best.get("origin") if best else None, "tier": best.get("tier") if best else None,
                    "archetype": g["archetype"], "family": g["family"], "control": ctrl}
    nc = {k: v for k, v in per.items() if not v["control"]}
    return {"solved": sum(v["solved"] for v in nc.values()), "of": len(nc),
            "rate": round(sum(v["solved"] for v in nc.values()) / max(1, len(nc)), 3), "per_pattern": per}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("gym"); ap.add_argument("--memory"); ap.add_argument("--playbooks", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    gym = json.load(open(a.gym)); mem = Memory(a.memory) if a.memory else None
    r = asyncio.run(score(gym, mem, a.playbooks))
    print(json.dumps({k: r[k] for k in ("solved", "of", "rate")}))
    for k, v in r["per_pattern"].items(): print(f"  {k:45} {v['archetype']:4} {v['family']:5} solved={v['solved']} {v['origin']} {v['tier']}")
    if a.out: json.dump(r, open(a.out, "w"), indent=1)
