"""Algorithmic recipe induction (main design §7.1) + playbook and skill plug-in application.

Generation-0 built-in capabilities (deliberately limited; L3 skills extend them):
  * html-isolation recipes (T1): CSS selectors from the isolation profile, raw HTML must contain content
  * embedded-JSON recipes (T1): <script type=application/json|ld+json> and `window.X = {...}` blobs
  * direct data-request recipes (T0): an XHR/fetch whose URL (and POST body) is a template over page-URL
    variables only. No chained values, no required headers, no cookies.
"""
import asyncio, json, re, hashlib, os, shutil, subprocess, sys, tempfile
from collections import defaultdict, OrderedDict
from urllib.parse import urlsplit
from bs4 import BeautifulSoup

from .recipe import run_recipe, offline_transport, html_lines, RecipeError, _path
from .util import equivalent, url_vars as _url_vars

# ------------------------------------------------------------------ helpers


def _norm(s):
    return " ".join(str(s).split())


def flatten(obj, prefix=""):
    """Yield (path, value) for scalar leaves; path uses [i] for list indices."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def gen_path(p):
    return re.sub(r"\[\d+\]", "[*]", p)


def parent(p):
    m = re.match(r"^(.*?)(\.[^.\[\]]+|\[\d+\])$", p)
    return m.group(1) if m else ""


def _containers(obj, prefix=""):
    """Yield (path, container) for dicts and lists."""
    if isinstance(obj, dict):
        yield prefix, obj
        for k, v in obj.items(): yield from _containers(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        yield prefix, obj
        for i, v in enumerate(obj): yield from _containers(v, f"{prefix}[{i}]")

# ------------------------------------------------------------------ JSON sources


def json_sources(sample):
    """Candidate JSON data sources in one capture. Returns list of dicts with a 'kind' and a recipe step spec."""
    cap = sample["capture"]; out = []
    reqs = cap["requests"]
    doc = reqs[0] if reqs else None
    page_html = doc.get("body") if doc else ""
    if page_html:
        soup = BeautifulSoup(page_html, "lxml")
        for sc in soup.find_all("script"):
            typ = (sc.get("type") or "").lower(); txt = sc.string or sc.get_text() or ""
            if typ in ("application/json", "application/ld+json"):
                try: data = json.loads(txt)
                except Exception: continue
                sel = f"script#{sc['id']}" if sc.get("id") else f'script[type="{typ}"]'
                out.append({"kind": "embedded", "data": data, "extract": {"css": sel, "attr": "text", "json": True}})
            elif typ in ("", "text/javascript", "module"):
                for m in re.finditer(r"window\.([A-Za-z_$][\w$]*)\s*=\s*(\{.*\}|\[.*\])\s*;?\s*$", txt, re.S):
                    try: data = json.loads(m.group(2))
                    except Exception: continue
                    rx = r"window\." + re.escape(m.group(1)) + r"\s*=\s*(\{.*\}|\[.*\])\s*;?\s*</script>"
                    out.append({"kind": "embedded", "data": data, "extract": {"regex": rx, "json": True}})
    for i, r in enumerate(reqs[1:], 1):
        if not r.get("body") or r.get("status") is None or not (200 <= r["status"] < 300): continue
        try: data = json.loads(r["body"])
        except Exception: continue
        out.append({"kind": "xhr", "data": data, "req_index": i, "request": r})
    return out


def map_lines(data, ref):
    """Map reference main-text lines to JSON paths. Returns (mapping list, coverage)."""
    leaves = [(p, v) for p, v in flatten(data) if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    by_val = defaultdict(list)
    for p, v in leaves: by_val[_norm(v)].append(p)
    html_leaves = [(p, v) for p, v in leaves if isinstance(v, str) and "<" in v and ">" in v]
    html_cache = {p: [_norm(x) for x in html_lines(v)] for p, v in html_leaves}
    multi = {p: [_norm(x) for x in re.split(r"\n+", v) if x.strip()] for p, v in leaves if isinstance(v, str) and "\n" in v}
    containers = list(_containers(data))
    lines = [l for l in ref["main_text"].split("\n") if l.strip()]
    mapping = []
    for line in lines:
        ln = _norm(line); hit = None
        if ln in by_val:
            hit = {"path": by_val[ln][0], "op": "value"}
        else:
            for p, hl in html_cache.items():
                if ln in hl: hit = {"path": p, "op": "html_text"}; break
            if not hit:
                for p, ml in multi.items():
                    if ln in ml: hit = {"path": p, "op": "split"}; break
        if not hit and ": " in ln:
            k, v = ln.split(": ", 1)
            for cp, c in containers:
                if isinstance(c, dict) and k in c and _norm(c[k]) == v and not isinstance(c[k], (dict, list)):
                    hit = {"path": cp, "op": "kv"}; break
                if isinstance(c, dict):
                    keys = [kk for kk, vv in c.items() if _norm(vv) == k]; vals = [kk for kk, vv in c.items() if _norm(vv) == v]
                    if keys and vals and keys[0] != vals[0]:
                        hit = {"path": parent(cp) if re.search(r"\[\d+\]$", cp) else cp, "op": "kvlist",
                               "kv": {"key": keys[0], "value": vals[0]}}; break
        mapping.append(hit)
    cov = sum(1 for h in mapping if h) / max(1, len(lines))
    return mapping, cov


def link_extractors(data, ref, page_url):
    """Find extractors (path / template) reproducing reference per-page links from JSON."""
    origin = "{s.scheme}://{s.netloc}".format(s=urlsplit(page_url))
    ref_links = ref.get("links", [])
    exs = []
    if not ref_links: return exs, 1.0
    leaves = [(p, v) for p, v in flatten(data) if isinstance(v, str)]
    found = set()
    for L in ref_links:
        path = urlsplit(L).path + (("?" + urlsplit(L).query) if urlsplit(L).query else "")
        for p, v in leaves:
            if v == L or v == path:
                ex = {"path": gen_path(p), "abs": True}
                if ex not in exs: exs.append(ex)
                found.add(L); break
    # templates over lists of scalar ids/slugs, e.g. "related": [12, 13]
    from .recipe import fill as _fill
    for cp, c in _containers(data):
        if not (isinstance(c, list) and c and all(isinstance(o, (str, int)) and not isinstance(o, bool) for o in c)): continue
        for L in ref_links:
            if L in found: continue
            lp = urlsplit(L).path + (("?" + urlsplit(L).query) if urlsplit(L).query else "")
            for v in c:
                pat = r"(?<![A-Za-z0-9])" + re.escape(str(v)) + r"(?![A-Za-z0-9])"
                if not re.search(pat, lp): continue
                ex = {"path": gen_path(cp) + "[*]", "template": "{{origin}}" + re.sub(pat, "{{value}}", lp, count=1)}
                produced = {_fill(ex["template"], {"origin": origin, "value": x}) for x in c}
                hits = produced & set(ref_links)
                if len(hits) >= max(1, len(c) // 2):
                    if ex not in exs: exs.append(ex)
                    found |= hits
                break
    # templates over lists of objects
    for cp, c in _containers(data):
        if not (isinstance(c, list) and c and all(isinstance(o, dict) for o in c)): continue
        for L in ref_links:
            if L in found: continue
            lp = urlsplit(L).path + (("?" + urlsplit(L).query) if urlsplit(L).query else "")
            for o in c:
                tpl = lp; used = False
                scal = sorted(((k, str(v)) for k, v in o.items() if isinstance(v, (str, int)) and len(str(v)) >= 1),
                              key=lambda kv: -len(kv[1]))
                for k, v in scal:
                    pat = r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])"
                    if re.search(pat, tpl):
                        tpl = re.sub(pat, "{{" + k + "}}", tpl, count=1); used = True
                if not used: continue
                ex = {"path": gen_path(cp) + "[*]", "template": "{{origin}}" + tpl}
                try:
                    from .recipe import fill
                    produced = {fill(ex["template"], {"origin": origin, **{k: v for k, v in oo.items() if not isinstance(v, (dict, list))}}) for oo in c}
                except Exception:
                    continue
                hits = produced & set(ref_links)
                if len(hits) >= max(1, len(c) // 2):
                    if ex not in exs: exs.append(ex)
                    found |= hits
                    break
    return exs, len(found & set(ref_links)) / len(ref_links)


def template_value(s, vars_, skip=("page_url", "query", "path_noslash")):
    """Replace occurrences of page-URL variable values in s with {{var}}. Longest values first."""
    if s is None: return None
    out = s
    items = sorted(((k, str(v)) for k, v in vars_.items() if k not in skip and v and len(str(v)) >= 2),
                   key=lambda kv: (-len(kv[1]), kv[0]))
    if vars_.get("origin") and vars_["origin"] in out: out = out.replace(vars_["origin"], "{{origin}}")
    for k, v in items:
        if k == "origin": continue
        pat = r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])"
        out = re.sub(pat, "{{" + k + "}}", out)
    return out


def _rid(recipe):
    return hashlib.sha1(json.dumps(recipe["steps"], sort_keys=True).encode() + json.dumps(recipe["output"], sort_keys=True).encode()).hexdigest()[:10]

# ------------------------------------------------------------------ candidate generators (gen-0)


def _main_extractors(mapping, step):
    exs = []
    for h in mapping:
        if not h: continue
        if h["op"] == "value": ex = {"step": step, "path": gen_path(h["path"])}
        elif h["op"] == "html_text": ex = {"step": step, "path": gen_path(h["path"]), "html_text": True}
        elif h["op"] == "split": ex = {"step": step, "path": gen_path(h["path"]), "split": "\\n+"}
        elif h["op"] == "kv": ex = {"step": step, "path": gen_path(h["path"]), "kv": True}
        else: ex = {"step": step, "path": gen_path(h["path"]), "kv": h["kv"]}
        if ex not in exs: exs.append(ex)
    return exs


def json_candidates(samples):
    s0 = samples[0]; cands = []
    for src in json_sources(s0):
        mapping, cov = map_lines(src["data"], s0["ref"])
        if cov < 0.8: continue
        ref_title = _norm(s0["ref"]["title"])
        tpaths = [p for p, v in flatten(src["data"]) if isinstance(v, str) and _norm(v) == ref_title]
        lex, lcov = link_extractors(src["data"], s0["ref"], s0["url"])
        if src["kind"] == "embedded":
            steps = [{"name": "page", "method": "GET", "url": "{{page_url}}", "parse": "html",
                      "extract": {}}]
            base = dict(src["extract"]); base["step"] = "page"
            def wrap(ex):
                e = dict(base); e.update({k: v for k, v in ex.items() if k != "step"}); return e
            out = {"title": wrap({"path": gen_path(tpaths[0]), "first": True}) if tpaths else {"step": "page", "css": "title", "first": True},
                   "main_text": [wrap(e) for e in _main_extractors(mapping, "page")],
                   "links": [wrap(e) for e in lex]}
            tier = "T1"
        else:
            r = src["request"]
            url_t = template_value(r["url"], s0["vars"]); body_t = template_value(r.get("post_data"), s0["vars"])
            hdrs = {}
            ct = {k.lower(): v for k, v in (r.get("headers") or {}).items()}.get("content-type")
            if r.get("post_data") and ct: hdrs["Content-Type"] = ct
            steps = [{"name": "data", "method": r["method"], "url": url_t, "headers": hdrs, "body": body_t, "parse": "json"}]
            out = {"title": {"step": "data", "path": gen_path(tpaths[0]), "first": True} if tpaths else None,
                   "main_text": _main_extractors(mapping, "data"),
                   "links": [dict(e, step="data") for e in lex]}
            tier = "T0"
        rec = {"tier": tier, "steps": steps, "output": {k: v for k, v in out.items() if v is not None}, "origin": "analyzer"}
        cands.append(rec)
    return cands


def css_of(sig):
    parts = []
    for p in sig.split(">"):
        m = re.match(r"^([a-z0-9]+)(#[^.]+)?((?:\.[^.]+)*)$", p)
        if not m: parts.append(p); continue
        tag, idv, cls = m.groups()
        sel = tag + (idv or "") + "".join("." + c for c in cls.split(".") if c)
        parts.append(sel)
    return " > ".join(parts)


def html_candidate(samples, profile):
    if not profile or profile.get("n", 0) < 3: return []
    s0 = samples[0]
    title_ex = {"step": "page", "css": "title", "first": True}
    from .isolate import blocks
    _, tb, _ = blocks(s0["capture"]["final_html"], s0["url"])
    for sig, t in tb:
        if sig in profile["var_text"] and _norm(t) == _norm(s0["ref"]["title"]):
            title_ex = {"step": "page", "css": css_of(sig), "first": True}; break
    main = [{"step": "page", "css": css_of(s), "attr": "text"} for s in profile["var_text"]]
    links = [{"step": "page", "css": css_of(s), "attr": "href", "abs": True} for s in profile["var_links"]]
    return [{"tier": "T1", "origin": "analyzer", "steps": [{"name": "page", "method": "GET", "url": "{{page_url}}", "parse": "html"}],
             "output": {"title": title_ex, "main_text": main, "links": links}}]

# ------------------------------------------------------------------ verification


async def verify_offline(recipe, samples, f1_min=0.9, link_min=0.95):
    """Run recipe against each sample's capture. Returns (n_pass, details)."""
    details = []
    for s in samples:
        try:
            rec, _ = await run_recipe(recipe, s["url"], offline_transport(s["capture"]), s["vars"])
            ok, d = equivalent(rec, s["ref"], f1_min, link_min)
        except Exception as e:
            ok, d = False, {"error": str(e)[:160]}
        details.append({"url": s["url"], "ok": ok, **d})
    return sum(1 for d in details if d["ok"]), details

# ------------------------------------------------------------------ playbooks & skills


def playbook_matches(pb, samples):
    det = pb.get("detector") or {}
    for s in samples[:1]:
        html = (s["capture"]["requests"][0].get("body") or "") if s["capture"]["requests"] else ""
        if det.get("html_regex") and not re.search(det["html_regex"], html, re.S): return False
        if det.get("request_url_regex") and not any(re.search(det["request_url_regex"], r["url"]) for r in s["capture"]["requests"][1:]):
            return False
        if det.get("page_url_regex") and not re.search(det["page_url_regex"], s["url"]): return False
    return bool(det)


def evidence_payload(samples, max_body=60000):
    out = []
    for s in samples:
        reqs = [{k: (v[:max_body] if isinstance(v, str) and k == "body" else v) for k, v in r.items()
                 if k in ("url", "method", "headers", "post_data", "status", "mime", "body", "type")} for r in s["capture"]["requests"]]
        out.append({"url": s["url"], "vars": s["vars"], "ref": s["ref"], "requests": reqs,
                    "final_html": (s["capture"].get("final_html") or "")[:max_body]})
    return out


SANDBOX_PRELUDE = r'''
import sys, json, resource, socket
resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
def _no_net(*a, **k): raise PermissionError("network disabled in skill sandbox")
socket.socket = _no_net; socket.create_connection = _no_net
'''


def run_skill_sandboxed(skill_path, samples, timeout=60):
    """Run an agent-written skill plug-in in isolation. Returns (list of recipes, error or None).

    docker mode (default when docker is available): network-less, read-only, unprivileged container with
      memory/CPU/pid limits and all capabilities dropped; the skill sees only its own source and the evidence on stdin.
    process mode (fallback): separate interpreter with CPU/memory/file-size rlimits and the socket module disabled.
      This is NOT a security boundary; see SECURITY.md.
    """
    from .config import sandbox_mode, SANDBOX_IMAGE
    payload = json.dumps(evidence_payload(samples)); skill_path = os.path.abspath(skill_path)
    runner_body = """
import importlib.util
spec = importlib.util.spec_from_file_location("skill", SKILL)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
ev = json.loads(sys.stdin.read())
res = mod.generate(ev)
print(json.dumps(res if isinstance(res, list) else []))
"""
    d = tempfile.mkdtemp(prefix="rsicrawl-skill-")
    try:
        with open(os.path.join(d, "skill.py"), "w") as f: f.write(open(skill_path).read())
        mode = sandbox_mode()
        if mode == "docker":
            with open(os.path.join(d, "runner.py"), "w") as f:
                f.write("import sys, json\nSKILL='/w/skill.py'\n" + runner_body)
            os.chmod(d, 0o755)
            for fn in ("skill.py", "runner.py"): os.chmod(os.path.join(d, fn), 0o644)
            cmd = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--memory", "1g", "--cpus", "1",
                   "--pids-limit", "64", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
                   "-v", f"{d}:/w:ro", SANDBOX_IMAGE, "python", "-I", "/w/runner.py"]
        else:
            with open(os.path.join(d, "runner.py"), "w") as f:
                f.write(SANDBOX_PRELUDE + f"SKILL={os.path.join(d, 'skill.py')!r}\n" + runner_body)
            cmd = [sys.executable, "-I", os.path.join(d, "runner.py")]
        p = subprocess.run(cmd, input=payload, capture_output=True, text=True, timeout=timeout,
                           cwd=tempfile.gettempdir(), env={"PATH": "/usr/local/bin:/usr/bin:/bin"})
        if p.returncode != 0: return [], p.stderr[-800:]
        res = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else []
        return [r for r in res if isinstance(r, dict) and "steps" in r and "output" in r], None
    except Exception as e:
        return [], str(e)[:300]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ main entry


TIER_RANK = {"T0": 0, "T1": 1, "T2": 2}


async def analyze(samples, profile, playbooks=(), skills=(), use_builtin=True, exclude=()):
    """Return (best_recipe or None, report). Candidates from playbooks, skills, then built-ins; verified offline."""
    report = {"tried": [], "skill_errors": []}
    cands = []
    for pb in playbooks:
        if playbook_matches(pb, samples):
            r = json.loads(json.dumps(pb["recipe"])); r["origin"] = f"playbook:{pb['id']}"; cands.append(r)
    for sk in skills:
        rs, err = await asyncio.to_thread(run_skill_sandboxed, sk["path"], samples)
        if err: report["skill_errors"].append({"skill": sk["id"], "error": err[-300:]})
        for r in rs: r["origin"] = f"skill:{sk['id']}"; r.setdefault("tier", "T0"); cands.append(r)
    if use_builtin:
        try: cands += json_candidates(samples)
        except Exception as e: report["builtin_error"] = str(e)[:200]
        cands += html_candidate(samples, profile)
    best = None
    exclude = set(exclude)
    for c in cands:
        if _rid(c) in exclude:
            report["tried"].append({"origin": c.get("origin"), "tier": c.get("tier"), "passed": 0, "of": len(samples),
                                    "detail": [{"rejected": "failed a live trial earlier"}]}); continue
        n, det = await verify_offline(c, samples)
        report["tried"].append({"origin": c.get("origin"), "tier": c.get("tier"), "passed": n, "of": len(samples),
                                "detail": det[:2]})
        if n == len(samples):
            if best is None or TIER_RANK.get(c["tier"], 9) < TIER_RANK.get(best["tier"], 9):
                best = c
    if best is not None: best["id"] = _rid(best)
    return best, report
