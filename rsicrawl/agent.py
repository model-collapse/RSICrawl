"""The crawl-engineering agent (main design §7.3): Claude via Bedrock, manual tool-use loop, bounded episodes.

Job types: investigate / repair (L1), distill (L2), improve (L3), reflect (lessons).
All judging (verify, gym) is done by code outside the agent's control.
"""
import asyncio, json, re, time, uuid, traceback
from anthropic import Anthropic, AnthropicBedrock
from . import config
import anthropic

from . import recipe as recipe_mod
from .recipe import run_recipe, offline_transport, live_transport
from .util import BudgetedHTTP, equivalent, url_vars

MODELS = config.model_ids()
PRICE = {"opus": (4.0, 20.0, 0.2, 5.0), "sonnet": (2.0, 10.0, 0.1, 2.5), "haiku": (0.1, 0.5, 0.01, 0.125)}  # in, out, cache read, cache write per MTok

_client = None


def client():
    global _client
    if _client is None:
        if config.llm_provider() == "bedrock":
            _client = AnthropicBedrock(aws_region=config.aws_region(), max_retries=4, timeout=600)
        else:
            _client = Anthropic(max_retries=4, timeout=600)
    return _client


class Spend:
    def __init__(self, cap_usd):
        self.cap = cap_usd; self.usd = 0.0; self.inp = 0; self.out = 0

    def add(self, model, usage):
        pin, pout, pcr, pcw = PRICE[model]
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0; cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        usd = (usage.input_tokens * pin + usage.output_tokens * pout + cr * pcr + cw * pcw) / 1e6
        self.usd += usd; self.inp += usage.input_tokens + cr + cw; self.out += usage.output_tokens
        return usd

    @property
    def exhausted(self): return self.usd >= self.cap


def _trunc(s, n=4000):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"... [truncated {len(s) - n} chars]"


async def _call(model, system, messages, tools, spend, max_tokens=16000):
    def do():
        kw = dict(model=MODELS[model], max_tokens=max_tokens, system=system, messages=messages)
        if tools: kw["tools"] = tools
        return client().messages.create(**kw)
    for attempt in range(5):
        try:
            resp = await asyncio.to_thread(do); break
        except (anthropic.InternalServerError, anthropic.RateLimitError, anthropic.APIConnectionError) as e:
            if attempt == 4: raise
            await asyncio.sleep(5 * (attempt + 1))
    usd = spend.add(model, resp.usage)
    return resp, usd

# =============================================================== shared prompt pieces


DSL_DOC = recipe_mod.__doc__

CONSTRAINTS = """Hard rules (enforced by code; violations simply fail):
- Obey robots.txt. Never call URLs robots.txt disallows; the HTTP tool refuses them.
- Do not bypass bot walls/CAPTCHAs, do not log in, do not forge cryptographic signatures. If a data request needs a
  client-computed signature or the site serves a challenge, call give_up with the right category.
- Every live request you make draws from the site's politeness budget. Prefer offline evidence (captures) first.
- A recipe is only accepted if it reproduces the reference records (main_text token F1 >= 0.9, per-page link recall
  >= 0.95, title match) on the samples, both offline and live. You cannot change how this is judged."""

# =============================================================== Investigate / Repair


INVESTIGATE_SYSTEM = """You are the crawl-engineering agent of a self-improving web crawler. Pages of one URL pattern share a
template; their main content comes from one data source (a JSON API the page calls, JSON embedded in the HTML, or
the HTML itself). Your job in this episode: find how to get this pattern's main content DIRECTLY (without a browser)
and express it as a declarative recipe, then submit it.

You get evidence: several pages of the pattern that were fully rendered in a headless browser, with every network
request/response captured, the raw HTML, the rendered DOM, and the reference record (title, main_text, per-page
links) extracted from the rendered DOM. The built-in algorithmic analyzer already failed on this pattern (its
report is in the brief), so expect something non-trivial: chained values (a token/id from the page HTML or from an
earlier response used in a later request), a required header, a cookie set by an earlier request, a multi-step
lookup, a POST body, HTML inside JSON, etc.

Procedure: (1) orient: list samples, look at the requests of one sample, find which response contains the main
text (search_bodies with a distinctive phrase from the reference). (2) Work out how that request's URL/body/headers
derive from the page URL and from earlier responses; compare across samples. (3) Draft a recipe and dry-run it with
run_recipe (offline first, then live). (4) Fix and iterate. (5) submit_recipe. Prefer the cheapest recipe (fewest
requests; avoid fetching the page HTML if the data request does not need anything from it).
Be efficient: you have a limited number of turns.

""" + CONSTRAINTS + "\n\nRecipe DSL reference:\n" + DSL_DOC

INVESTIGATE_TOOLS = [
    {"name": "list_samples", "description": "List the rendered sample pages of this pattern with URL variables and a reference preview.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_requests", "description": "List the captured network requests of one sample (index 0 is the page document).",
     "input_schema": {"type": "object", "properties": {"sample": {"type": "integer"}}, "required": ["sample"], "additionalProperties": False}},
    {"name": "get_request", "description": "Full detail of one captured request: request headers, POST body, status, response headers, a slice of the response body.",
     "input_schema": {"type": "object", "properties": {"sample": {"type": "integer"}, "index": {"type": "integer"},
                                                       "offset": {"type": "integer"}, "length": {"type": "integer"}},
                      "required": ["sample", "index"], "additionalProperties": False}},
    {"name": "get_html", "description": "Slice of a sample's raw HTML (as served) or rendered DOM.",
     "input_schema": {"type": "object", "properties": {"sample": {"type": "integer"}, "which": {"type": "string", "enum": ["raw", "rendered"]},
                                                       "offset": {"type": "integer"}, "length": {"type": "integer"}},
                      "required": ["sample", "which"], "additionalProperties": False}},
    {"name": "get_reference", "description": "Full reference record (title, main_text, links) of a sample.",
     "input_schema": {"type": "object", "properties": {"sample": {"type": "integer"}}, "required": ["sample"], "additionalProperties": False}},
    {"name": "search_bodies", "description": "Search all samples' raw HTML and response bodies (and request URLs/headers/bodies) for a literal string or regex. Returns hits with context.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "regex": {"type": "boolean"}},
                      "required": ["query"], "additionalProperties": False}},
    {"name": "http_request", "description": "Make one live HTTP request (robots-checked, rate-budgeted). Use sparingly to test hypotheses (e.g. which headers are required).",
     "input_schema": {"type": "object", "properties": {"method": {"type": "string"}, "url": {"type": "string"},
                                                       "headers": {"type": "object"}, "body": {"type": "string"}},
                      "required": ["method", "url"], "additionalProperties": False}},
    {"name": "run_recipe", "description": "Dry-run a recipe on samples. mode=offline serves requests from the captures (exact URL match; does not check headers/cookies); mode=live makes real requests. Returns per-sample equivalence and the record of the first sample.",
     "input_schema": {"type": "object", "properties": {"recipe": {"type": "object"}, "mode": {"type": "string", "enum": ["offline", "live"]},
                                                       "samples": {"type": "array", "items": {"type": "integer"}}},
                      "required": ["recipe", "mode"], "additionalProperties": False}},
    {"name": "search_lessons", "description": "Search lessons learned in earlier episodes (symptom -> what worked).",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}},
    {"name": "submit_recipe", "description": "Submit the final recipe. It is verified offline and live on all samples by the judge; if accepted the episode ends.",
     "input_schema": {"type": "object", "properties": {"recipe": {"type": "object"}, "rationale": {"type": "string"}},
                      "required": ["recipe", "rationale"], "additionalProperties": False}},
    {"name": "give_up", "description": "End the episode without a recipe. category: signed_request | bot_wall | robots | no_direct_source | other.",
     "input_schema": {"type": "object", "properties": {"category": {"type": "string"}, "reason": {"type": "string"}},
                      "required": ["category", "reason"], "additionalProperties": False}},
]

STD_HEADERS = {"user-agent", "accept", "accept-language", "accept-encoding", "referer", "sec-ch-ua", "sec-ch-ua-full-version-list",
               "connection", "host", "content-length", "origin"}


class PatternTools:
    def __init__(self, pattern_id, samples, budget, robots, memory):
        self.pid, self.samples, self.budget, self.robots, self.memory = pattern_id, samples, budget, robots, memory
        self.live_requests = 0; self.submitted = None; self.ended = None

    def _s(self, i):
        if not (0 <= i < len(self.samples)): raise ValueError(f"sample must be 0..{len(self.samples) - 1}")
        return self.samples[i]

    async def list_samples(self):
        return [{"sample": i, "url": s["url"], "url_vars": {k: v for k, v in s["vars"].items() if k not in ("page_url",)},
                 "ref_title": s["ref"]["title"], "ref_main_text_preview": s["ref"]["main_text"][:300],
                 "n_ref_links": len(s["ref"]["links"]), "n_requests": len(s["capture"]["requests"])} for i, s in enumerate(self.samples)]

    async def get_requests(self, sample):
        out = []
        for j, r in enumerate(self._s(sample)["capture"]["requests"]):
            nh = {k: v for k, v in (r.get("headers") or {}).items() if k.lower() not in STD_HEADERS}
            out.append({"index": j, "method": r["method"], "url": r["url"], "type": r.get("type"), "status": r.get("status"),
                        "mime": r.get("mime"), "body_len": len(r.get("body") or ""), "nonstandard_request_headers": nh,
                        "has_post_body": bool(r.get("post_data"))})
        return out

    async def get_request(self, sample, index, offset=0, length=3000):
        r = self._s(sample)["capture"]["requests"][index]
        body = r.get("body") or ""
        return {"method": r["method"], "url": r["url"], "request_headers": r.get("headers"), "post_data": r.get("post_data"),
                "status": r.get("status"), "response_headers": r.get("resp_headers"),
                "body_slice": body[offset:offset + min(length, 8000)], "body_len": len(body)}

    async def get_html(self, sample, which, offset=0, length=4000):
        s = self._s(sample)
        h = (s["capture"]["requests"][0].get("body") or "") if which == "raw" else s["capture"].get("final_html", "")
        return {"slice": h[offset:offset + min(length, 8000)], "len": len(h)}

    async def get_reference(self, sample):
        return self._s(sample)["ref"]

    async def search_bodies(self, query, regex=False):
        hits = []
        rx = re.compile(query if regex else re.escape(query))
        for i, s in enumerate(self.samples):
            for j, r in enumerate(s["capture"]["requests"]):
                for where, txt in (("url", r["url"]), ("request_headers", json.dumps(r.get("headers") or {})),
                                   ("post_data", r.get("post_data") or ""), ("response_body", r.get("body") or "")):
                    for m in rx.finditer(txt or ""):
                        a, b = max(0, m.start() - 120), min(len(txt), m.end() + 120)
                        hits.append({"sample": i, "request": j, "url": r["url"][:200], "where": where, "context": txt[a:b]})
                        if len(hits) >= 25: return hits
        return hits

    async def http_request(self, method, url, headers=None, body=None):
        http = BudgetedHTTP(self.budget, self.robots, learning=True)
        try:
            r = await http.request(method, url, headers=headers or {}, content=body.encode() if body else None)
            self.live_requests += http.requests
            return {"status": r.status_code, "headers": dict(r.headers), "body_preview": r.text[:3000],
                    "set_cookie": r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else r.headers.get("set-cookie")}
        except PermissionError as e:
            return {"error": str(e)}
        finally:
            await http.aclose()

    async def run_recipe(self, recipe, mode, samples=None):
        idx = samples if samples else list(range(len(self.samples)))
        res = []; first_record = None
        for i in idx:
            s = self._s(i)
            if mode == "live":
                http = BudgetedHTTP(self.budget, self.robots, learning=True); tx = live_transport(http)
            else:
                http = None; tx = offline_transport(s["capture"])
            try:
                rec, _ = await run_recipe(recipe, s["url"], tx, s["vars"])
                ok, d = equivalent(rec, s["ref"])
                if first_record is None: first_record = {k: (v[:600] if isinstance(v, str) else v[:12] if isinstance(v, list) else v) for k, v in rec.items()}
                res.append({"sample": i, "ok": ok, **d})
            except Exception as e:
                res.append({"sample": i, "ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            finally:
                if http: self.live_requests += http.requests; await http.aclose()
        return {"results": res, "first_record": first_record}

    async def search_lessons(self, query):
        return self.memory.search_lessons(query)

    async def submit_recipe(self, recipe, rationale):
        if not isinstance(recipe, dict) or "steps" not in recipe or "output" not in recipe:
            return {"accepted": False, "reason": "recipe must have steps and output"}
        off = await self.run_recipe(recipe, "offline"); live = await self.run_recipe(recipe, "live")
        ok = all(r["ok"] for r in live["results"]) and len(live["results"]) == len(self.samples)
        offline_ok = all(r["ok"] for r in off["results"])
        if ok:
            recipe.setdefault("tier", "T1" if any("{{page_url}}" in (st.get("url") or "") for st in recipe["steps"]) else "T0")
            self.submitted = {"recipe": recipe, "rationale": rationale}; self.ended = "submitted"
            return {"accepted": True, "offline_all_pass": offline_ok}
        return {"accepted": False, "live": live["results"], "offline": off["results"]}

    async def give_up(self, category, reason):
        self.ended = f"give_up:{category}"; self.give_up_reason = reason
        return {"ok": True}


async def run_tool_loop(job, system, brief, tools_schema, impl, spend, model="opus", max_turns=28, on_trace=None):
    """Generic manual tool-use loop. Returns trace dict."""
    messages = [{"role": "user", "content": brief}]
    trace = {"job": job, "model": model, "turns": [], "usd": 0.0, "input_tokens": 0, "output_tokens": 0}
    t0 = time.time()
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    for turn in range(max_turns):
        if spend.exhausted: trace["end"] = "spend_cap"; break
        try:
            resp, usd = await _call(model, system_blocks, messages, tools_schema, spend)
        except anthropic.BadRequestError as e:
            trace["end"] = f"api_error: {str(e)[:200]}"; break
        except Exception as e:
            trace["end"] = f"api_error: {type(e).__name__} {str(e)[:200]}"; break
        trace["usd"] += usd; trace["input_tokens"] += resp.usage.input_tokens; trace["output_tokens"] += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})
        texts = [b.text for b in resp.content if b.type == "text"]
        calls = [b for b in resp.content if b.type == "tool_use"]
        trace["turns"].append({"text": " ".join(texts)[:1500], "calls": [{"name": c.name, "input": _trunc(c.input, 1500)} for c in calls]})
        if resp.stop_reason == "refusal": trace["end"] = "refusal"; break
        if not calls:
            if resp.stop_reason == "max_tokens": trace["end"] = "max_tokens"; break
            messages.append({"role": "user", "content": "Continue: use the tools; finish with submit/give_up."}); continue
        results = []
        for c in calls:
            fn = getattr(impl, c.name, None)
            try:
                out = await fn(**(c.input or {})) if fn else {"error": f"unknown tool {c.name}"}
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": _trunc(out, 9000)})
            except Exception as e:
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": f"{type(e).__name__}: {e}", "is_error": True})
            trace["turns"][-1].setdefault("results", []).append(_trunc(results[-1]["content"], 600))
        messages.append({"role": "user", "content": results})
        if getattr(impl, "ended", None): trace["end"] = impl.ended; break
    else:
        trace["end"] = "max_turns"
    trace["duration_s"] = round(time.time() - t0, 1)
    return trace


async def investigate(pattern_id, samples, analyzer_report, budget, robots, memory, spend, prior_recipe=None,
                      failure=None, use_lessons=True, model="opus"):
    tools = PatternTools(pattern_id, samples, budget, robots, memory)
    schema = [t for t in INVESTIGATE_TOOLS if use_lessons or t["name"] != "search_lessons"]
    brief = {"job": "repair" if prior_recipe else "investigate", "pattern_id": pattern_id, "n_samples": len(samples),
             "analyzer_report": [{k: v for k, v in t.items() if k != "detail"} | {"detail": t.get("detail", [])[:1]} for t in analyzer_report.get("tried", [])][:6],
             "markers": analyzer_report.get("markers"),
             "candidates_that_passed_offline_but_failed_live": analyzer_report.get("live_rejections")}
    if prior_recipe: brief["previous_recipe_now_failing"] = prior_recipe; brief["failure"] = failure
    if use_lessons:
        brief["relevant_lessons"] = memory.search_lessons(" ".join([pattern_id] + list(analyzer_report.get("markers") or [])), k=5)
    trace = await run_tool_loop(brief["job"], INVESTIGATE_SYSTEM, json.dumps(brief, indent=1), schema, tools, spend, model=model)
    trace["pattern_id"] = pattern_id; trace["live_requests"] = tools.live_requests
    trace["result"] = tools.submitted; trace["give_up_reason"] = getattr(tools, "give_up_reason", None)
    return trace

# =============================================================== Reflect


REFLECT_SYSTEM = """You write concise, reusable lessons for a web-crawling agent's memory, from the trace of one episode.
Return ONLY a JSON object: {"symptom": str, "site_markers": [str], "hypotheses_tried": [str], "what_worked": str,
"generality": "site|family|general", "tags": [str]}. Keep each string under 300 characters."""


async def reflect(trace, markers, spend):
    brief = json.dumps({"job": trace.get("job"), "pattern": trace.get("pattern_id"), "end": trace.get("end"),
                        "markers": markers, "result": trace.get("result"), "give_up_reason": trace.get("give_up_reason"),
                        "turns": [{"text": t["text"][:400], "calls": t["calls"][:4], "results": t.get("results", [])[:3]} for t in trace["turns"]][-14:]})
    try:
        resp, usd = await _call("haiku", REFLECT_SYSTEM, [{"role": "user", "content": _trunc(brief, 60000)}], None, spend, max_tokens=2000)
        txt = "".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", txt, re.S)
        lesson = json.loads(m.group(0)) if m else {"what_worked": txt[:300]}
        lesson["pattern_id"] = trace.get("pattern_id"); lesson["outcome"] = trace.get("end")
        return lesson, usd
    except Exception as e:
        return None, 0.0

# =============================================================== Distill (L2)


DISTILL_SYSTEM = """You are the crawl-engineering agent distilling cross-site knowledge (a PLAYBOOK). You are given
several working recipes from DIFFERENT sites that share technology markers, plus evidence access. Write ONE playbook:
{"id": short_snake_case, "description": str, "detector": {"html_regex"?: str, "request_url_regex"?: str,
"page_url_regex"?: str}, "recipe": <recipe in the DSL that works on any site of this kind>}.
The recipe must not contain site-specific literals (hostnames, ports, site names, specific ids, tokens): derive
everything from page-URL variables ({{origin}}, {{path}}, {{seg0}}, {{last}}, ...) and chained extraction from the page
HTML/earlier responses. The detector must be specific enough not to fire on unrelated sites (it is matched on the raw
HTML of the page document / the request URLs the page made). Test with test_playbook (it runs the playbook offline on
every pattern's samples, including held-out ones) and then submit_playbook. A playbook is accepted only if it
reproduces the reference records on at least 2 different sites.

""" + CONSTRAINTS + "\n\nRecipe DSL reference:\n" + DSL_DOC


class DistillTools:
    def __init__(self, group, memory, analyzer_mod):
        self.group, self.memory, self.an = group, memory, analyzer_mod
        self.ended = None; self.submitted = None
        self.ptools = {g["pattern_id"]: PatternTools(g["pattern_id"], g["samples"], None, None, memory) for g in group}

    async def list_group(self):
        return [{"pattern_id": g["pattern_id"], "site": g["pattern_id"].split("/")[0], "markers": g["markers"],
                 "recipe": g["recipe"], "sample_urls": [s["url"] for s in g["samples"]]} for g in self.group]

    async def get_requests(self, pattern_id, sample):
        return await self.ptools[pattern_id].get_requests(sample)

    async def get_html(self, pattern_id, sample, which, offset=0, length=4000):
        return await self.ptools[pattern_id].get_html(sample, which, offset, length)

    async def test_playbook(self, playbook):
        res = []
        for g in self.group:
            match = self.an.playbook_matches(playbook, g["samples"])
            n, det = await self.an.verify_offline(playbook["recipe"], g["samples"]) if match else (0, [])
            res.append({"pattern_id": g["pattern_id"], "detector_matched": match, "passed": n, "of": len(g["samples"]),
                        "detail": det[:2]})
        return res

    async def submit_playbook(self, playbook):
        res = await self.test_playbook(playbook)
        sites = {r["pattern_id"].split("/")[0] for r in res if r["detector_matched"] and r["passed"] == r["of"]}
        if len(sites) >= 2:
            self.submitted = playbook; self.ended = "submitted"; return {"accepted": True, "sites": sorted(sites)}
        return {"accepted": False, "results": res}

    async def give_up(self, category, reason):
        self.ended = f"give_up:{category}"; return {"ok": True}


DISTILL_TOOLS = [
    {"name": "list_group", "description": "The recipes (one per site) to generalize, with markers and sample URLs.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_requests", "description": "Captured requests of a sample of one pattern.",
     "input_schema": {"type": "object", "properties": {"pattern_id": {"type": "string"}, "sample": {"type": "integer"}}, "required": ["pattern_id", "sample"], "additionalProperties": False}},
    {"name": "get_html", "description": "Slice of raw or rendered HTML of a sample.",
     "input_schema": {"type": "object", "properties": {"pattern_id": {"type": "string"}, "sample": {"type": "integer"}, "which": {"type": "string", "enum": ["raw", "rendered"]},
                                                       "offset": {"type": "integer"}, "length": {"type": "integer"}}, "required": ["pattern_id", "sample", "which"], "additionalProperties": False}},
    {"name": "test_playbook", "description": "Run a playbook offline on every pattern of the group; per-pattern detector match and pass counts.",
     "input_schema": {"type": "object", "properties": {"playbook": {"type": "object"}}, "required": ["playbook"], "additionalProperties": False}},
    {"name": "submit_playbook", "description": "Submit the playbook; accepted if it works on >= 2 sites.",
     "input_schema": {"type": "object", "properties": {"playbook": {"type": "object"}}, "required": ["playbook"], "additionalProperties": False}},
    {"name": "give_up", "description": "End without a playbook.",
     "input_schema": {"type": "object", "properties": {"category": {"type": "string"}, "reason": {"type": "string"}}, "required": ["category", "reason"], "additionalProperties": False}},
]


async def distill(group, memory, analyzer_mod, spend):
    tools = DistillTools(group, memory, analyzer_mod)
    brief = json.dumps({"job": "distill", "n_patterns": len(group), "shared_markers": sorted(set.intersection(*[set(g["markers"]) for g in group]))})
    trace = await run_tool_loop("distill", DISTILL_SYSTEM, brief, DISTILL_TOOLS, tools, spend, max_turns=18)
    trace["result"] = tools.submitted
    return trace

# =============================================================== Improve (L3)


IMPROVE_SYSTEM = """You are the crawl-engineering agent improving the crawler's own METHOD. The crawler's algorithmic
analyzer turns rendered sample pages of a URL pattern into recipes. When it fails, an expensive LLM episode is needed.
Your job: write a SKILL, a Python plug-in that the analyzer runs on every new pattern, so that a whole CLASS of
problems that previously needed an LLM episode is solved automatically from now on.

A skill is a Python module defining `generate(evidence) -> list[recipe]`. `evidence` is a list of samples:
{"url", "vars" (page-URL variables usable as {{name}} in recipes), "ref" (reference record: title, main_text,
links), "requests" (captured requests; index 0 is the page document: url, method, headers, post_data, status, mime,
body), "final_html"}. It returns candidate recipes (DSL below); the judge verifies each on held-out samples, so
returning several candidates is fine. It runs sandboxed: standard library only (json, re, html, urllib.parse ...),
no network, no files, 20 s CPU. Write GENERAL code: no site-specific literals; infer structure from the evidence
(e.g. find which response contains the reference text, find where each varying part of that request's URL/body/
headers comes from: page-URL variables, the page HTML, or an earlier response, and emit extract steps for them).

You see: the failure classes (lessons and agent solutions for patterns the analyzer could not solve), the analyzer's
built-in capabilities, and a gym of train patterns. run_gym(code) scores your skill: for each gym pattern, the
analyzer (built-ins + existing skills + yours) analyzes 3 samples and the resulting recipe is checked on the
held-out samples. submit_skill is accepted only if the number of solved gym patterns increases and no pattern that
was solved before becomes unsolved.

""" + "Recipe DSL reference:\n" + DSL_DOC


class ImproveTools:
    def __init__(self, gym, memory, analyzer_mod, baseline):
        self.gym, self.memory, self.an, self.baseline = gym, memory, analyzer_mod, baseline
        self.ended = None; self.submitted = None; self.attempt = 0

    async def list_failures(self):
        unsolved = [p for p, ok in self.baseline["per_pattern"].items() if not ok]
        agent_solutions = {pid: e["versions"][-1] for pid, e in self.memory.all_recipes().items()
                           if e["versions"] and str(e["versions"][-1].get("origin", "")).startswith("agent")}
        return {"unsolved_gym_patterns": unsolved, "agent_solutions_for_some_patterns": agent_solutions,
                "lessons": self.memory.lessons()[-25:], "baseline_solved": self.baseline["solved"], "gym_size": len(self.gym)}

    async def builtin_capabilities(self):
        import inspect
        return {"doc": self.an.__doc__, "existing_skills": [{"id": s["id"], "description": s.get("description")} for s in self.memory.skills()]}

    async def get_evidence(self, pattern_id, sample, part="requests", index=0, offset=0, length=3000):
        g = self.gym[pattern_id]; s = g["samples"][sample]
        if part == "requests":
            return [{"index": j, "method": r["method"], "url": r["url"], "status": r.get("status"), "body_len": len(r.get("body") or ""),
                     "headers": {k: v for k, v in (r.get("headers") or {}).items() if k.lower() not in STD_HEADERS}, "post_data": r.get("post_data")}
                    for j, r in enumerate(s["capture"]["requests"])]
        if part == "request_body":
            return (s["capture"]["requests"][index].get("body") or "")[offset:offset + min(length, 8000)]
        if part == "ref": return s["ref"]
        if part == "vars": return s["vars"]
        return {"error": "part must be requests|request_body|ref|vars"}

    async def run_gym(self, code):
        import tempfile, os
        self.attempt += 1
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir="/tmp") as f:
            f.write(code); path = f.name
        try:
            res = await gym_score(self.gym, self.memory, self.an, extra_skill={"id": "candidate", "path": path})
        finally:
            os.unlink(path)
        newly = [p for p, ok in res["per_pattern"].items() if ok and not self.baseline["per_pattern"].get(p)]
        lost = [p for p, ok in res["per_pattern"].items() if not ok and self.baseline["per_pattern"].get(p)]
        return {"solved": res["solved"], "baseline_solved": self.baseline["solved"], "newly_solved": newly, "regressed": lost,
                "skill_errors": res["skill_errors"][:3], "per_pattern_reports": res["reports"]}

    async def submit_skill(self, skill_id, code, description):
        r = await self.run_gym(code)
        if r["newly_solved"] and not r["regressed"]:
            self.submitted = {"id": re.sub(r"\W", "_", skill_id)[:40], "code": code, "description": description, "gym": r}
            self.ended = "submitted"; return {"accepted": True, **{k: r[k] for k in ("solved", "newly_solved")}}
        return {"accepted": False, **{k: r[k] for k in ("solved", "baseline_solved", "newly_solved", "regressed", "skill_errors")}}

    async def give_up(self, category, reason):
        self.ended = f"give_up:{category}"; return {"ok": True}


IMPROVE_TOOLS = [
    {"name": "list_failures", "description": "Gym patterns the analyzer cannot solve, recipes the agent wrote by hand for patterns, and recent lessons.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "builtin_capabilities", "description": "What the analyzer already does, and existing skills.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_evidence", "description": "Inspect a gym pattern sample. part: requests | request_body (with index, offset, length) | ref | vars.",
     "input_schema": {"type": "object", "properties": {"pattern_id": {"type": "string"}, "sample": {"type": "integer"}, "part": {"type": "string"},
                                                       "index": {"type": "integer"}, "offset": {"type": "integer"}, "length": {"type": "integer"}},
                      "required": ["pattern_id", "sample"], "additionalProperties": False}},
    {"name": "run_gym", "description": "Score a candidate skill (full Python source defining generate(evidence)) on the gym.",
     "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"], "additionalProperties": False}},
    {"name": "submit_skill", "description": "Submit the skill; accepted only if it solves new gym patterns with no regression.",
     "input_schema": {"type": "object", "properties": {"skill_id": {"type": "string"}, "code": {"type": "string"}, "description": {"type": "string"}},
                      "required": ["skill_id", "code", "description"], "additionalProperties": False}},
    {"name": "give_up", "description": "End without a skill.",
     "input_schema": {"type": "object", "properties": {"category": {"type": "string"}, "reason": {"type": "string"}}, "required": ["category", "reason"], "additionalProperties": False}},
]


async def gym_score(gym, memory, analyzer_mod, extra_skill=None, n_train=3):
    """Gym (main design §7.4): per pattern, analyze first n_train samples, check recipe on the held-out rest."""
    skills = memory.skills() + ([extra_skill] if extra_skill else [])
    per, reports, errs = {}, {}, []
    for pid, g in gym.items():
        tr, ho = g["samples"][:n_train], g["samples"][n_train:]
        best, rep = await analyzer_mod.analyze(tr, g.get("profile"), playbooks=(), skills=skills, use_builtin=True)
        errs += rep.get("skill_errors", [])
        ok = False
        if best is not None and ho:
            n, _ = await analyzer_mod.verify_offline(best, ho); ok = n == len(ho)
        per[pid] = ok
        reports[pid] = {"solved": ok, "best_origin": best.get("origin") if best else None,
                        "tried": [(t["origin"], t["passed"], t["of"]) for t in rep["tried"]][:8]}
    return {"solved": sum(per.values()), "per_pattern": per, "reports": reports, "skill_errors": errs}


async def improve(gym, memory, analyzer_mod, spend):
    baseline = await gym_score(gym, memory, analyzer_mod)
    tools = ImproveTools(gym, memory, analyzer_mod, baseline)
    brief = json.dumps({"job": "improve", "gym_size": len(gym), "baseline_solved": baseline["solved"],
                        "unsolved": [p for p, ok in baseline["per_pattern"].items() if not ok]})
    trace = await run_tool_loop("improve", IMPROVE_SYSTEM, brief, IMPROVE_TOOLS, tools, spend, max_turns=30)
    trace["result"] = tools.submitted; trace["baseline"] = {"solved": baseline["solved"], "per_pattern": baseline["per_pattern"]}
    return trace
