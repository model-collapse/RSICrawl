"""One L3 generation step (benchmark design §5.3): run an Improve episode on the TRAIN gym with the given memory;
if the gym judge accepts the skill, add it to the memory (in place).

python -m bench.improve --memory DIR --gym runs/g11_train.json --out runs/gen1.json [--spend-cap 10]
"""
import argparse, asyncio, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rsicrawl import agent as ag, analyzer as an
from rsicrawl.memory import Memory


async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--memory", required=True); ap.add_argument("--gym", required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--spend-cap", type=float, default=10.0)
    ap.add_argument("--attempts", type=int, default=2)
    a = ap.parse_args()
    mem = Memory(a.memory); gym = json.load(open(a.gym))
    gym = {k: v for k, v in gym.items() if v["archetype"] not in ("A10", "A11", "A12")}
    spend = ag.Spend(a.spend_cap); results = []
    for i in range(a.attempts):
        t0 = time.time()
        trace = await ag.improve(gym, mem, an, spend)
        eid = f"improve-{int(t0)}"; mem.write_trace(eid, trace)
        res = trace.get("result")
        row = {"attempt": i, "episode": eid, "end": trace.get("end"), "usd": round(trace.get("usd", 0), 3), "turns": len(trace["turns"]),
               "baseline_solved": trace["baseline"]["solved"], "duration_s": trace.get("duration_s")}
        if res:
            path = mem.put_skill(res["id"], res["code"], {"description": res["description"], "source_episode": eid,
                                                           "gym_solved": res["gym"]["solved"], "newly_solved": res["gym"]["newly_solved"]})
            row.update({"skill": res["id"], "solved_after": res["gym"]["solved"], "newly_solved": res["gym"]["newly_solved"]})
            results.append(row); print(json.dumps(row)); break
        results.append(row); print(json.dumps(row))
    json.dump({"results": results, "spend_usd": round(spend.usd, 3)}, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    asyncio.run(main())
