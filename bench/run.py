"""Benchmark runner (benchmark design §5, prototype scale).

python -m bench.run --forge runs/forge7 --system SYS --split test --out runs/r1 [--memory-from DIR] [--limit N]
                    [--drift] [--rate 2] [--spend-cap 30]
"""
import argparse, asyncio, json, os, shutil, subprocess, sys, time, socket
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rsicrawl.engine import Engine
from rsicrawl.memory import Memory
from rsicrawl.config import lightpanda_bin, model_ids, llm_provider

CONTROL = 18999


def start_forge(forge, control=CONTROL, subres=None):
    env = dict(os.environ)
    if subres is not None: env["SITEFORGE_SUBRES"] = str(subres)
    p = subprocess.Popen([sys.executable, "-m", "siteforge.server", "--forge", forge, "--control-port", str(control)],
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        try: socket.create_connection(("127.0.0.1", control), timeout=0.2).close(); break
        except OSError: time.sleep(0.05)
    time.sleep(0.5)
    return p


def site_lists(forge, splits, limit=None, only=None):
    sites = json.load(open(os.path.join(forge, "sites.json")))
    out = {}
    for s in sites:
        if s["split"] not in splits: continue
        if only and s["site_id"] not in only: continue
        urls = [l.strip() for l in open(os.path.join(forge, "urls", f"{s['site_id']}.txt")) if l.strip()]
        out[s["site_id"]] = urls[:limit] if limit else urls
    return out, {s["site_id"]: s for s in sites}


DRIFT_PLAN = [("D2", "spa", "A3"), ("D5", "next", "A3"), ("D3", "next", "A2"), ("D4", "spa", "A3"), ("D1", "nuxt", "A2"), ("D6", "nuxt", "A1")]


def make_drift(sites_meta, lists, at_frac=0.5):
    """Apply one drift event per eligible site once that site's crawl passes at_frac of its page list."""
    plan = {}
    used = set()
    for ev, fam, arch in DRIFT_PLAN:
        for sid, m in sites_meta.items():
            if sid in lists and sid not in used and m["family"] == fam and m["archetype"] == arch:
                plan[sid] = ev; used.add(sid); break

    async def drift(engine):
        done = set()
        async with httpx.AsyncClient() as c:
            while len(done) < len(plan):
                await asyncio.sleep(1.0)
                counts = {}
                with open(os.path.join(engine.run_dir, "events.jsonl")) as f:
                    for l in f:
                        e = json.loads(l)
                        if "url" in e and "tier" in e:
                            port = e["url"].split(":")[2].split("/")[0]; counts[port] = counts.get(port, 0) + 1
                for sid, ev in plan.items():
                    if sid in done: continue
                    port = str(sites_meta[sid]["port"])
                    if counts.get(port, 0) >= int(len(lists[sid]) * at_frac):
                        r = await c.post(f"http://127.0.0.1:{CONTROL}/drift", json={"site_id": sid, "event": ev})
                        engine.emit({"event": "drift_applied", "site_id": sid, "drift": ev, "at_page": counts.get(port, 0),
                                     "applied": r.json().get("applied"), "t": time.time()})
                        done.add(sid)
    return drift, plan


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forge", required=True); ap.add_argument("--system", required=True)
    ap.add_argument("--split", default="test"); ap.add_argument("--out", required=True)
    ap.add_argument("--memory-from"); ap.add_argument("--limit", type=int); ap.add_argument("--only")
    ap.add_argument("--drift", action="store_true"); ap.add_argument("--rate", type=float, default=2.0)
    ap.add_argument("--spend-cap", type=float, default=30.0); ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-l3", action="store_true", help="freeze skills (no new skills; existing ones used)")
    ap.add_argument("--no-step-cache", action="store_true", help="disable per-site chain-value caching (v1 behaviour)")
    ap.add_argument("--subres", type=int, default=0, help="SiteForge sub-resources per page (0 = light pages, as in v1)")
    ap.add_argument("--keep-recipes", action="store_true", help="keep L1 recipes from --memory-from (default: drop)")
    a = ap.parse_args()
    if os.path.exists(a.out): shutil.rmtree(a.out)
    os.makedirs(a.out)
    memdir = os.path.join(a.out, "memory")
    if a.memory_from:
        shutil.copytree(a.memory_from, memdir)
        if not a.keep_recipes and os.path.exists(os.path.join(memdir, "recipes.json")): os.remove(os.path.join(memdir, "recipes.json"))
        # rewrite skill paths
        sp = os.path.join(memdir, "skills.json")
        if os.path.exists(sp):
            d = json.load(open(sp))
            for s in d.values(): s["path"] = os.path.join(memdir, "skills", os.path.basename(s["path"]))
            json.dump(d, open(sp, "w"), indent=1)
    enabled = {"SYS": ("recipes", "playbooks", "skills", "lessons"), "B4": ("recipes",), "B3": ("recipes",),
               "B0": (), "B2": ()}[a.system]
    mem = Memory(memdir, enabled=enabled)
    lists, meta = site_lists(a.forge, a.split.split(","), a.limit, a.only.split(",") if a.only else None)
    forge = start_forge(a.forge, subres=a.subres)
    try:
        async with httpx.AsyncClient() as c: await c.post(f"http://127.0.0.1:{CONTROL}/reset")
        eng = Engine(a.system, mem, a.out, rate=a.rate, spend_cap=a.spend_cap, seed=a.seed, step_cache=not a.no_step_cache)
        drift = None; plan = {}
        if a.drift: drift, plan = make_drift(meta, lists)
        t0 = time.time()
        res = await eng.run(lists, drift=drift)
        wall = time.time() - t0
        async with httpx.AsyncClient() as c:
            logs = (await c.get(f"http://127.0.0.1:{CONTROL}/log")).json()
        json.dump(logs, open(os.path.join(a.out, "server_log.json"), "w"))
        manifest = {"subres": a.subres, "step_cache": not a.no_step_cache, "system": a.system, "split": a.split, "forge": a.forge, "limit": a.limit, "rate": a.rate,
                    "memory_from": a.memory_from, "wall_s": round(wall, 1), "drift_plan": plan, "sites": list(lists),
                    "budget_used": dict(eng.budget.used), "learning_used": dict(eng.budget.learning_used), **res,
                    "lightpanda": subprocess.run([lightpanda_bin(), "version"], capture_output=True, text=True).stdout.strip(),
                    "models": model_ids(), "llm_provider": llm_provider()}
        json.dump(manifest, open(os.path.join(a.out, "manifest.json"), "w"), indent=1)
        print(json.dumps({k: manifest[k] for k in ("system", "split", "wall_s", "spend_usd")}))
    finally:
        forge.terminate(); forge.wait()


if __name__ == "__main__":
    asyncio.run(main())
