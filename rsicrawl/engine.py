"""Crawl plane + learning plane orchestration (main design §5-§7).

Modes (benchmark systems): B0 = Lightpanda render only; B2 = plain HTTP + isolation (no JS);
B3 = analyzer only (no agent); B4 = agent without memory (no playbooks/skills/lessons); SYS = full system.
"""
import asyncio, json, os, random, re, time, uuid
from collections import defaultdict, deque
from urllib.parse import urlsplit

from . import analyzer as an
from . import agent as ag
from .cdp import LightpandaServer, render
from .isolate import build_profile, reference_record
from .recipe import run_recipe, live_transport, RecipeError, StepCache
from .util import BudgetedHTTP, HostBudget, Robots, equivalent, url_pattern, url_vars, UA

CHALLENGE = re.compile(r"captcha|checking your browser|are you a robot|verify you are human", re.I)
MARKERS = [("__NEXT_DATA__", r"__NEXT_DATA__"), ("_next/data", r"/_next/data/"), ("__NUXT__", r"__NUXT__"),
           ("wp-json", r"/wp-json/"), ("ld+json", r"application/ld\+json"), ("graphql", r"/graphql"),
           ("csrf-meta", r"csrf-token"), ("api-session", r"/api/session"), ("api-config", r"/api/config"),
           ("data-post-id", r"data-post-id"), ("api-v", r"/api/v\d"), ("svc", r"/svc/")]


def markers_of(capture):
    txt = (capture["requests"][0].get("body") or "") if capture.get("requests") else ""
    txt += " " + " ".join(r["url"] for r in capture.get("requests", [])[1:])
    return sorted({name for name, rx in MARKERS if re.search(rx, txt)})


class PatternState:
    def __init__(self, pid):
        self.pid = pid; self.samples = []; self.profile = None; self.status = "learning"
        self.candidate = None; self.cand_pass = 0; self.cand_fail = 0
        self.analyzed_at = -1; self.investigating = False; self.agent_attempts = 0; self.slow_only_until = 0
        self.window = deque(maxlen=10); self.streak = 0; self.blocked = 0; self.render_reqs = deque([4], maxlen=5)
        self.markers = []; self.last_report = {}; self.seen = 0


class Engine:
    def __init__(self, mode, memory, run_dir, rate=2.0, n_samples=3, k_trials=2, shadow_floor=0.03,
                 agent_conc=4, spend_cap=30.0, seed=0, max_agent_attempts=2, distill=True, model="opus", step_cache=True, learn_share=0.15):
        self.mode, self.mem, self.run_dir = mode, memory, run_dir
        self.budget = HostBudget(rate_per_s=rate, burst=max(2, int(rate * 2)))
        self.robots = Robots(); self.n_samples, self.k = n_samples, k_trials; self.shadow_floor = shadow_floor
        self.patterns = {}; self.rng = random.Random(seed); self.spend = ag.Spend(spend_cap)
        self.agent_sem = asyncio.Semaphore(agent_conc); self.agent_tasks = set(); self.jobq = []
        self.max_agent_attempts = max_agent_attempts; self.do_distill = distill and mode == "SYS"; self.model = model
        self.agent_on = mode in ("SYS", "B4"); self.use_memory_kb = mode == "SYS"
        self.remaining = defaultdict(int); self.distilled_groups = set()
        os.makedirs(run_dir, exist_ok=True)
        self.ev_f = open(os.path.join(run_dir, "events.jsonl"), "a"); self.ep_f = open(os.path.join(run_dir, "episodes.jsonl"), "a")
        os.makedirs(os.path.join(run_dir, "evidence"), exist_ok=True)
        self.lp = None; self.step_cache = step_cache; self.caches = {}; self.learn_share = learn_share

    # ------------------------------------------------------------ events
    def emit(self, ev):
        self.ev_f.write(json.dumps(ev) + "\n"); self.ev_f.flush()

    def emit_episode(self, ep):
        self.ep_f.write(json.dumps(ep) + "\n"); self.ep_f.flush()

    # ------------------------------------------------------------ tiers
    async def t3(self, url, ps, learning=False):
        host = urlsplit(url).netloc
        wait = await self.budget.acquire(host, 1, learning=learning)
        t0 = time.monotonic()
        cap = await render(self.lp.ws, url, block=lambda u: not self.robots.allowed(u), idle_ms=250, timeout=25)
        n = len([r for r in cap["requests"] if urlsplit(r["url"]).netloc == host])
        self.budget.charge(host, max(0, n - 1), learning=learning)
        ps.render_reqs.append(n)
        return cap, int((time.monotonic() - t0) * 1000), int(wait * 1000), n, sum(len(r.get("body") or "") for r in cap["requests"])

    async def fast(self, url, recipe, learning=False, use_cache=False):
        """Run a recipe live. With use_cache (active recipes, v2), stable chain steps are skipped via a per-recipe
        StepCache + kept cookie jar; on failure the cache is cleared and the recipe is re-run in full once."""
        cache = None
        if use_cache and self.step_cache:
            cache = self.caches.setdefault((urlsplit(url).netloc, recipe.get("id") or id(recipe)), StepCache())
        t0 = time.monotonic(); total = {"requests": 0, "bytes": 0, "wait_s": 0.0}
        attempts = [True, False] if cache is not None else [False]
        err = None; rec = None; info = {}
        for with_cache in attempts:
            http = BudgetedHTTP(self.budget, self.robots, learning=learning,
                                cookies=cache.cookies if (with_cache and cache is not None) else None)
            try:
                rec, info = await run_recipe(recipe, url, live_transport(http), url_vars(url),
                                             cache=cache if with_cache else None)
                if cache is not None: cache.cookies = http.client.cookies
                err = None
            except Exception as e:
                rec, err = None, f"{type(e).__name__}: {str(e)[:160]}"
                if cache is not None and with_cache: cache.clear()
            finally:
                total["requests"] += http.requests; total["bytes"] += http.bytes; total["wait_s"] += http.wait_s
                await http.aclose()
            if err is None or not (with_cache and info is not None): break
        http.requests, http.bytes, http.wait_s = total["requests"], total["bytes"], total["wait_s"]
        http.elapsed = time.monotonic() - t0; http.info = info or {}
        return rec, err, http

    def is_challenge(self, cap):
        doc = cap["requests"][0] if cap.get("requests") else {}
        return (cap.get("status") in (403, 429, 503)) and bool(CHALLENGE.search((doc.get("body") or "") + (cap.get("final_html") or "")))

    # ------------------------------------------------------------ per page
    async def process(self, url):
        pid = url_pattern(url); ps = self.patterns.setdefault(pid, PatternState(pid)); ps.seen += 1
        self.remaining[pid] -= 1
        host = urlsplit(url).netloc
        base = {"url": url, "pattern_id": pid, "t": time.time()}
        if f"http://{host}" not in self.robots.cache:
            h = BudgetedHTTP(self.budget, self.robots); await self.robots.load(f"http://{host}", h.client); await h.aclose()
        if not self.robots.allowed(url):
            return self.emit({**base, "tier": "SKIP", "skip": "robots", "service_ms": 0, "queue_ms": 0, "requests": 0, "bytes": 0, "record": None})
        if ps.blocked >= 3:
            return self.emit({**base, "tier": "SKIP", "skip": "bot_wall", "blocked": True, "service_ms": 0, "queue_ms": 0, "requests": 0, "bytes": 0, "record": None})
        if self.mode == "B2":
            return await self.process_b2(url, ps, base)
        recipe = self.mem.recipe_for(pid) if self.mode != "B0" else None
        if recipe:
            rec, err, http = await self.fast(url, recipe, use_cache=True)
            if err is None:
                # drift signal (main design §7.4): partial extraction counts as a fast-path failure
                nlines = len([x for x in rec.get("main_text", "").split("\n") if x.strip()])
                typical = sorted(ps.fast_lines)[len(ps.fast_lines) // 2] if getattr(ps, "fast_lines", None) else None
                if recipe["output"].get("title") and not rec.get("title", "").strip():
                    err = "PartialExtraction: empty title"
                elif typical and nlines < 0.5 * typical:
                    err = f"PartialExtraction: {nlines} lines vs typical {typical}"
                else:
                    ps.fast_lines = (getattr(ps, "fast_lines", []) + [nlines])[-20:]
            svc = int(http.elapsed * 1000 - http.wait_s * 1000)
            tier = ("T1" if http.info.get("fetched_page") else "T0") if http.info else recipe.get("tier", "T0")
            ev = {**base, "tier": tier, "recipe_id": recipe.get("id"), "recipe_origin": recipe.get("origin"),
                  "steps_skipped": http.info.get("skipped", 0) if http.info else 0,
                  "service_ms": svc, "queue_ms": int(http.wait_s * 1000), "requests": http.requests, "bytes": http.bytes}
            if err is None:
                s = max(self.shadow_floor, 0.5 * (0.8 ** ps.streak))
                # design §3: learning + verification traffic is capped at LEARN_SHARE of the host's budget
                est = max(1, sum(ps.render_reqs) / len(ps.render_reqs))
                within_cap = self.budget.learning_used[host] + est <= self.learn_share * (self.budget.used[host] + est)
                if within_cap and self.rng.random() < s:
                    cap, ms, wait, n, b = await self.t3(url, ps, learning=True)
                    ref = reference_record(url, cap["final_html"], ps.profile)
                    ok, d = equivalent(rec, ref)
                    ev["shadow"] = {"ok": ok, **d}; ev["learning_requests"] = n
                    ps.window.append(ok); ps.streak = ps.streak + 1 if ok else 0
                    if not ok:
                        self.emit({**ev, "record": ref, "tier": "T3", "fast_failed": "shadow_mismatch", "service_ms": ev["service_ms"] + ms,
                                   "requests": ev["requests"] + n, "bytes": ev["bytes"] + b})
                        self.add_sample(ps, url, cap, ref)
                        return self.check_degrade(ps, f"shadow mismatch {d}", recipe)
                else:
                    ps.window.append(True); ps.streak += 1
                return self.emit({**ev, "record": rec})
            # fast path failed -> fall back to T3
            ps.window.append(False); ps.streak = 0
            cap, ms, wait, n, b = await self.t3(url, ps)
            ref = reference_record(url, cap["final_html"], ps.profile)
            self.emit({**ev, "tier": "T3", "fast_failed": err, "record": ref, "service_ms": svc + ms, "queue_ms": ev["queue_ms"] + wait,
                       "requests": ev["requests"] + n, "bytes": ev["bytes"] + b})
            self.add_sample(ps, url, cap, ref)
            return self.check_degrade(ps, err, recipe)
        # ---- no active recipe: slow path
        cap, ms, wait, n, b = await self.t3(url, ps)
        if self.is_challenge(cap):
            ps.blocked += 1
            return self.emit({**base, "tier": "T3", "blocked": True, "service_ms": ms, "queue_ms": wait, "requests": n, "bytes": b, "record": None})
        self.add_sample(ps, url, cap, None)
        ref = reference_record(url, cap["final_html"], ps.profile)
        self.emit({**base, "tier": "T3", "service_ms": ms, "queue_ms": wait, "requests": n, "bytes": b, "record": ref})
        if self.mode == "B0": return
        await self.learn(ps, url, cap, ref)

    async def process_b2(self, url, ps, base):
        http = BudgetedHTTP(self.budget, self.robots); t0 = time.monotonic()
        try:
            r = await http.request("GET", url); html = r.text; status = r.status_code
        except Exception as e:
            html, status = "", None
        cap = {"url": url, "final_html": html, "requests": [{"url": url, "method": "GET", "status": status, "body": html}]}
        if len(ps.samples) < 6: ps.samples.append({"url": url, "capture": cap, "vars": url_vars(url), "ref": None})
        if len(ps.samples) >= 3: ps.profile = build_profile([(s["url"], s["capture"]["final_html"]) for s in ps.samples])
        rec = reference_record(url, html, ps.profile)
        self.emit({**base, "tier": "T1", "service_ms": int((time.monotonic() - t0 - http.wait_s) * 1000), "queue_ms": int(http.wait_s * 1000),
                   "requests": http.requests, "bytes": http.bytes, "record": rec, "blocked": bool(status in (403, 429, 503))})
        await http.aclose()

    # ------------------------------------------------------------ learning
    def add_sample(self, ps, url, cap, ref):
        if any(s["url"] == url for s in ps.samples): return
        ps.samples.append({"url": url, "capture": cap, "vars": url_vars(url), "ref": ref})
        if len(ps.samples) > 8: ps.samples.pop(1)
        rebuilt = False
        if len(ps.samples) >= 3 and (ps.profile is None or len(ps.samples) <= 6):
            ps.profile = build_profile([(s["url"], s["capture"]["final_html"]) for s in ps.samples]); rebuilt = True
        if ps.profile:
            for s in (ps.samples if rebuilt else ps.samples[-1:]):
                s["ref"] = reference_record(s["url"], s["capture"]["final_html"], ps.profile)
        if not ps.markers: ps.markers = markers_of(cap)

    def check_degrade(self, ps, why, recipe):
        fails = sum(1 for x in ps.window if not x)
        if fails >= 2:
            self.mem.set_recipe_state(ps.pid, "degraded", note=why)
            ps.status = "learning"; ps.window.clear(); ps.streak = 0
            ps.samples = ps.samples[-2:]; ps.candidate = None; ps.cand_pass = 0; ps.analyzed_at = -1
            ps.agent_attempts = 0; ps.repair = {"recipe": recipe, "failure": why}; ps.pb_tried = False
            self.emit({"event": "degraded", "pattern_id": ps.pid, "why": why, "t": time.time()})

    async def learn(self, ps, url, cap, ref):
        # 1) candidate trial on this freshly rendered page (K held-out pages)
        if ps.candidate is not None:
            rec, err, http = await self.fast(url, ps.candidate, learning=True)
            ok, d = (False, {"error": err}) if err else equivalent(rec, ref)
            self.emit({"event": "trial", "pattern_id": ps.pid, "ok": ok, "detail": d, "origin": ps.candidate.get("origin"),
                       "requests": http.requests, "t": time.time()})
            if ok:
                ps.cand_pass += 1
                if ps.cand_pass >= self.k:
                    self.mem.put_recipe(ps.pid, ps.candidate, "active", note=f"activated after {ps.cand_pass} trials")
                    o = str(ps.candidate.get("origin", ""))
                    if o.startswith("playbook:"): self.mem.playbook_stat(o.split(":", 1)[1], True)
                    ps.status = "active"; ps.window.clear(); ps.streak = 0; ps.candidate = None
                    self.emit({"event": "activated", "pattern_id": ps.pid, "origin": o, "tier": self.mem.recipe_for(ps.pid).get("tier"), "t": time.time()})
                return
            o = str(ps.candidate.get("origin", ""))
            if o.startswith("playbook:"): self.mem.playbook_stat(o.split(":", 1)[1], False)
            # remember live rejections: offline checks cannot see cookie/header requirements, so the same
            # candidate must not be re-proposed; the failure is handed to the agent as evidence
            ps.rejected = getattr(ps, "rejected", {}); ps.rejected[ps.candidate.get("id") or an._rid(ps.candidate)] = {"origin": o, "error": d.get("error") if isinstance(d, dict) else str(d)}
            ps.candidate = None; ps.cand_pass = 0; ps.analyzed_at = -1
        # 2a) L2 playbooks can propose a candidate from fewer samples than the analyzer needs
        if (self.use_memory_kb and len(ps.samples) == 2 and not getattr(ps, "pb_tried", False) and not ps.investigating):
            ps.pb_tried = True
            pbs = self.mem.playbooks()
            if pbs:
                best, rep = await an.analyze(ps.samples, ps.profile, playbooks=pbs, skills=(), use_builtin=False, exclude=getattr(ps, "rejected", {}).keys())
                self.emit({"event": "analyzed", "pattern_id": ps.pid, "phase": "playbooks", "found": best is not None,
                           "origin": best.get("origin") if best else None, "tried": [(t["origin"], t["passed"], t["of"]) for t in rep["tried"]], "t": time.time()})
                if best is not None:
                    ps.candidate = best; ps.cand_pass = 0; return
        # 2) analyzer when enough samples (re-run after new samples arrive)
        if len(ps.samples) >= self.n_samples and ps.analyzed_at != len(ps.samples) and not ps.investigating:
            ps.analyzed_at = len(ps.samples)
            pbs = self.mem.playbooks() if self.use_memory_kb else []
            sks = self.mem.skills() if self.use_memory_kb else []
            best, rep = await an.analyze(ps.samples, ps.profile, playbooks=pbs, skills=sks, exclude=getattr(ps, "rejected", {}).keys())
            rep["markers"] = ps.markers; rep["live_rejections"] = list(getattr(ps, "rejected", {}).values())[-3:]; ps.last_report = rep
            self.save_evidence(ps)
            self.emit({"event": "analyzed", "pattern_id": ps.pid, "found": best is not None, "origin": best.get("origin") if best else None,
                       "tried": [(t["origin"], t["passed"], t["of"]) for t in rep["tried"]], "t": time.time()})
            if best is not None:
                ps.candidate = best; ps.cand_pass = 0; return
            if self.agent_on and ps.agent_attempts < self.max_agent_attempts and time.time() >= ps.slow_only_until:
                self.enqueue_investigate(ps)

    def save_evidence(self, ps):
        p = os.path.join(self.run_dir, "evidence", re.sub(r"[^\w.-]", "_", ps.pid) + ".json")
        with open(p, "w") as f:
            json.dump({"pattern_id": ps.pid, "markers": ps.markers, "profile": ps.profile,
                       "samples": [{"url": s["url"], "vars": s["vars"], "ref": s["ref"],
                                    "capture": {"url": s["capture"]["url"], "final_html": s["capture"]["final_html"],
                                                "requests": s["capture"]["requests"]}} for s in ps.samples]}, f)

    # ------------------------------------------------------------ orchestrator
    def enqueue_investigate(self, ps):
        ps.investigating = True; ps.agent_attempts += 1
        value = max(0, self.remaining[ps.pid]) * max(1, sum(ps.render_reqs) / len(ps.render_reqs))
        t = asyncio.create_task(self.run_investigate(ps, value))
        self.agent_tasks.add(t); t.add_done_callback(self.agent_tasks.discard)

    async def run_investigate(self, ps, value):
        # value-ranked admission: higher value waits less (simple priority via sleep jitter + semaphore)
        await asyncio.sleep(0 if value > 50 else 2)
        async with self.agent_sem:
            if self.spend.exhausted: ps.investigating = False; return
            samples = list(ps.samples); repair = getattr(ps, "repair", None)
            t0 = time.time()
            try:
                trace = await ag.investigate(ps.pid, samples, ps.last_report, self.budget, self.robots, self.mem, self.spend,
                                             prior_recipe=repair and repair["recipe"], failure=repair and repair["failure"],
                                             use_lessons=self.use_memory_kb, model=self.model)
            except Exception as e:
                import traceback; trace = {"job": "investigate", "end": f"crash {e}", "tb": traceback.format_exc()[-1500:], "usd": 0, "turns": []}
            eid = uuid.uuid4().hex[:10]; trace["episode_id"] = eid; trace["markers"] = ps.markers; trace["pattern_id"] = ps.pid
            self.mem.write_trace(eid, trace)
            ok = bool(trace.get("result"))
            self.emit_episode({"episode_id": eid, "job": trace.get("job"), "pattern_id": ps.pid, "model": self.model,
                               "input_tokens": trace.get("input_tokens", 0), "output_tokens": trace.get("output_tokens", 0),
                               "usd": round(trace.get("usd", 0), 4), "requests": trace.get("live_requests", 0),
                               "outcome": trace.get("end"), "turns": len(trace.get("turns", [])), "duration_s": round(time.time() - t0, 1),
                               "value": value})
            if ok:
                r = trace["result"]["recipe"]; r["origin"] = f"agent:{eid}"; r["id"] = an._rid(r)
                ps.candidate = r; ps.cand_pass = 0
                ps.repair = None
            elif str(trace.get("end", "")).startswith("give_up"):
                ps.slow_only_until = time.time() + 3600
            ps.investigating = False
            if self.use_memory_kb:
                lesson, usd = await ag.reflect(trace, ps.markers, self.spend)
                if lesson: self.mem.add_lesson(lesson)
                self.emit_episode({"episode_id": eid + "-r", "job": "reflect", "pattern_id": ps.pid, "model": "haiku", "usd": round(usd, 5),
                                   "outcome": "lesson" if lesson else "none"})
            if ok and self.do_distill: self.maybe_distill()

    def maybe_distill(self):
        """L2: when >=2 active recipes on different sites share markers and no playbook covers them, distill."""
        recs = self.mem.all_recipes(); groups = defaultdict(list)
        for pid, e in recs.items():
            if e["state"] != "active" or pid not in self.patterns: continue
            ps = self.patterns[pid]
            if not ps.markers or str(e["versions"][-1].get("origin", "")).startswith("playbook"): continue
            groups[tuple(ps.markers)].append(pid)
        for key, pids in groups.items():
            sites = {p.split("/")[0] for p in pids}
            if len(sites) < 2 or key in self.distilled_groups: continue
            self.distilled_groups.add(key)
            group = [{"pattern_id": p, "markers": self.patterns[p].markers, "recipe": recs[p]["versions"][-1],
                      "samples": self.patterns[p].samples} for p in pids]
            t = asyncio.create_task(self.run_distill(group)); self.agent_tasks.add(t); t.add_done_callback(self.agent_tasks.discard)

    async def run_distill(self, group):
        async with self.agent_sem:
            if self.spend.exhausted: return
            t0 = time.time()
            try: trace = await ag.distill(group, self.mem, an, self.spend)
            except Exception as e: trace = {"job": "distill", "end": f"crash {e}", "usd": 0, "turns": []}
            eid = uuid.uuid4().hex[:10]; self.mem.write_trace(eid, trace)
            if trace.get("result"):
                pb = trace["result"]; pb["id"] = re.sub(r"\W", "_", str(pb.get("id", eid)))[:40]; pb["source_episode"] = eid
                self.mem.put_playbook(pb)
            self.emit_episode({"episode_id": eid, "job": "distill", "model": self.model, "usd": round(trace.get("usd", 0), 4),
                               "input_tokens": trace.get("input_tokens", 0), "output_tokens": trace.get("output_tokens", 0),
                               "outcome": trace.get("end"), "turns": len(trace.get("turns", [])), "duration_s": round(time.time() - t0, 1),
                               "patterns": [g["pattern_id"] for g in group]})

    # ------------------------------------------------------------ run
    async def host_worker(self, urls):
        for u in urls:
            try:
                await self.process(u)
            except Exception as e:
                import traceback
                self.emit({"url": u, "pattern_id": url_pattern(u), "tier": "ERR", "error": f"{type(e).__name__}: {e}",
                           "tb": traceback.format_exc()[-800:], "service_ms": 0, "queue_ms": 0, "requests": 0, "bytes": 0, "record": None})

    async def run(self, url_lists, drain_agents=True, drift=None):
        """url_lists: {site_id: [urls]}. drift: optional async callable(engine) run concurrently (benchmark P4)."""
        for urls in url_lists.values():
            for u in urls: self.remaining[url_pattern(u)] += 1
        self.lp = LightpandaServer(max_conn=64)
        try:
            tasks = [asyncio.create_task(self.host_worker(urls)) for urls in url_lists.values()]
            dtask = asyncio.create_task(drift(self)) if drift else None
            await asyncio.gather(*tasks)
            if dtask: dtask.cancel()
            if drain_agents and self.agent_tasks:
                await asyncio.gather(*list(self.agent_tasks), return_exceptions=True)
        finally:
            self.lp.close(); self.ev_f.flush(); self.ep_f.flush()
        return {"spend_usd": round(self.spend.usd, 3)}
