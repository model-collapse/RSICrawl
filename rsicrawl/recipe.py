"""Declarative recipe DSL + interpreter (main design §6.4).

Recipe JSON:
{
  "id": str, "pattern_id": str, "tier": "T0"|"T1", "origin": "analyzer|agent|playbook:<id>|skill:<id>",
  "steps": [ {"name": "page", "method": "GET", "url": "{{page_url}}", "headers": {..}, "body": null,
              "parse": "html|json|text", "extract": {"var": <extractor>, ...}}, ... ],
  "output": {"title": <extractor>, "main_text": [<extractor>, ...], "fields": {k: <extractor>},
             "links": [<extractor>, ...]}
}
Placeholders are {{name}}: page-URL variables (see util.url_vars) plus variables extracted by earlier steps.

<extractor> (ops applied in this order; each produces a list of values):
  "step": step name (default: last step)
  "css": selector, "attr": "text"|"html"|<attribute>        (HTML steps)
  "regex": pattern (group 1 if present, else whole match)     (raw text of the step)
  "json": true            parse each value as JSON (JS object literal tolerated if it is JSON)
  "path": "a.b[*].c"      JSON path: dot keys, [*] wildcard, [n] index
  "html_text": true       strip HTML to text lines (one per block element)
  "split": "\\n+"         split each string value by this regex into several lines
  "kv": true | {"key": "k", "value": "v"}   dict -> "k: v" lines; or list of objects using given keys
  "template": "{{origin}}/items/{{id}}/{{slug}}"   format each (dict) value into a string (links)
  "abs": true             resolve relative URLs against the page URL
  "first": true           keep only the first value
Network-behaviour primitives are limited to: chained values (extract), cookies (kept automatically per page),
headers/body templates, and {{now}} (unix seconds). No cryptographic signing (v1).
"""
import json, re, time
from urllib.parse import urljoin
from bs4 import BeautifulSoup

_PH = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


class RecipeError(Exception):
    pass


def fill(t, v):
    if t is None or not isinstance(t, str): return t
    def rep(m):
        k = m.group(1)
        if k == "now": return str(int(time.time()))
        if k not in v: raise RecipeError(f"missing variable {k}")
        return str(v[k])
    return _PH.sub(rep, t)


def _path(obj, path):
    cur = [obj]
    for part in re.findall(r"[^.\[\]]+|\[\*\]|\[\d+\]", path or ""):
        nxt = []
        for o in cur:
            if part == "[*]":
                if isinstance(o, list): nxt.extend(o)
                elif isinstance(o, dict): nxt.extend(o.values())
            elif part.startswith("["):
                i = int(part[1:-1])
                if isinstance(o, list) and -len(o) <= i < len(o): nxt.append(o[i])
            elif isinstance(o, dict) and part in o:
                nxt.append(o[part])
        cur = nxt
    return cur


def html_lines(h):
    soup = BeautifulSoup(h or "", "lxml")
    blocks = soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "pre", "blockquote", "td"])
    if not blocks: return [" ".join(soup.get_text(" ").split())] if soup.get_text().strip() else []
    return [" ".join(b.get_text(" ").split()) for b in blocks if b.get_text().strip()]


def _loads(x):
    if not isinstance(x, str): return x
    s = x.strip().rstrip(";")
    try: return json.loads(s)
    except Exception:
        raise RecipeError("json parse failed")


def apply_extractor(ex, steps, page_url, vars_):
    st = steps.get(ex.get("step")) if ex.get("step") else steps.get("__last__")
    if st is None: raise RecipeError(f"unknown step {ex.get('step')}")
    vals = None
    if "css" in ex:
        soup = st.get("_soup") or BeautifulSoup(st["text"], "lxml"); st["_soup"] = soup
        attr = ex.get("attr", "text"); vals = []
        for el in soup.select(ex["css"]):
            if attr == "text": vals.append(" ".join(el.get_text(" ").split()))
            elif attr == "html": vals.append(el.decode_contents())
            else:
                a = el.get(attr)
                if a is not None: vals.append(a)
    elif "regex" in ex:
        vals = []
        for m in re.finditer(ex["regex"], st["text"], re.S):
            vals.append(m.group(1) if m.groups() else m.group(0))
    else:
        vals = [st["json"] if st.get("json") is not None else st["text"]]
    if ex.get("json"): vals = [_loads(v) for v in vals]
    if "path" in ex:
        out = []
        for v in vals:
            if isinstance(v, str): v = _loads(v)
            out.extend(_path(v, ex["path"]))
        vals = out
    if ex.get("html_text"):
        out = []
        for v in vals: out.extend(html_lines(v if isinstance(v, str) else json.dumps(v)))
        vals = out
    if ex.get("split"):
        out = []
        for v in vals:
            out.extend(x for x in re.split(ex["split"], v if isinstance(v, str) else str(v)) if x.strip())
        vals = out
    if ex.get("kv"):
        kv = ex["kv"]; out = []
        for v in vals:
            if isinstance(v, dict) and kv is True: out.extend(f"{k}: {x}" for k, x in v.items())
            elif isinstance(v, list) and isinstance(kv, dict):
                out.extend(f"{o.get(kv['key'])}: {o.get(kv['value'])}" for o in v if isinstance(o, dict))
            elif isinstance(v, dict) and isinstance(kv, dict):
                out.append(f"{v.get(kv['key'])}: {v.get(kv['value'])}")
        vals = out
    if "template" in ex:
        out = []
        for v in vals:
            ctx = dict(vars_); ctx.update({k: x for k, x in v.items() if not isinstance(x, (dict, list))} if isinstance(v, dict) else {"value": v})
            out.append(fill(ex["template"], ctx))
        vals = out
    if ex.get("abs"): vals = [urljoin(page_url, str(v)) for v in vals]
    if ex.get("first"): vals = vals[:1]
    return vals


def _as_text(v):
    if isinstance(v, (dict, list)): return json.dumps(v, ensure_ascii=False)
    return "" if v is None else str(v)


class StepCache:
    """Per-site cache of chain values (prototype v2). A step is skipped when (a) nothing later reads its response
    directly, and (b) its extracted variables were identical on the last `stable_n` live pages (or, for a step with no
    extract, i.e. one that only sets cookies, it succeeded `stable_n` times and the site cookie jar is kept).
    Any failure while using the cache clears it and the caller re-runs the recipe in full."""
    def __init__(self, stable_n=3):
        self.n = stable_n; self.obs = {}; self.cookies = None

    def usable(self, name):
        o = self.obs.get(name, [])
        return len(o) >= self.n and all(x == o[-1] for x in o[-self.n:])

    def get(self, name):
        return self.obs[name][-1]

    def observe(self, name, vals):
        self.obs.setdefault(name, []).append(vals); self.obs[name] = self.obs[name][-self.n:]

    def clear(self):
        self.obs.clear(); self.cookies = None


def _referenced_steps(recipe):
    """Steps whose *response* is read by a later extractor or by the output (these can never be skipped)."""
    names = [st.get("name", f"s{i + 1}") for i, st in enumerate(recipe["steps"])]
    ref = {names[-1]} if names else set()
    def walk(ex, default):
        if isinstance(ex, dict): ref.add(ex.get("step") or default)
    out = recipe.get("output", {})
    for ex in [out.get("title")] + list(out.get("main_text", [])) + list((out.get("fields") or {}).values()) + list(out.get("links", [])):
        walk(ex, names[-1] if names else None)
    for i, st in enumerate(recipe["steps"]):
        for ex in (st.get("extract") or {}).values():
            if isinstance(ex, dict) and ex.get("step") and ex["step"] != names[i]: ref.add(ex["step"])
    return ref


async def run_recipe(recipe, page_url, transport, url_vars, cache=None):
    """transport: async (method, url, headers, body) -> (status, text). Returns (record, info).
    info: steps executed, steps skipped via cache, fetched_page (page HTML requested -> tier T1, else T0)."""
    vars_ = dict(url_vars); steps = {}; info = {"steps": 0, "skipped": 0, "fetched_page": False}
    ref = _referenced_steps(recipe) if cache is not None else set()
    for i, st in enumerate(recipe["steps"]):
        name = st.get("name", f"s{i + 1}")
        if cache is not None and name not in ref and cache.usable(name):
            vars_.update(cache.get(name)); info["skipped"] += 1
            continue
        if st["url"] in ("{{page_url}}",): info["fetched_page"] = True
        url = fill(st["url"], vars_)
        headers = {k: fill(v, vars_) for k, v in (st.get("headers") or {}).items()}
        body = fill(st.get("body"), vars_)
        status, text = await transport(st.get("method", "GET"), url, headers, body)
        info["steps"] += 1
        if status is None or not (200 <= status < 300):
            raise RecipeError(f"step {st.get('name')} HTTP {status} {url}")
        rec = {"text": text, "status": status}
        if st.get("parse") == "json":
            try: rec["json"] = json.loads(text)
            except Exception: raise RecipeError(f"step {st.get('name')} not JSON")
        steps[st.get("name", f"s{info['steps']}")] = rec; steps["__last__"] = rec
        got = {}
        for var, ex in (st.get("extract") or {}).items():
            vals = apply_extractor(ex, steps, page_url, vars_)
            if not vals: raise RecipeError(f"extract {var} empty")
            vars_[var] = got[var] = _as_text(vals[0])
        if cache is not None: cache.observe(name, got)
    out = recipe["output"]
    t = apply_extractor(out["title"], steps, page_url, vars_) if out.get("title") else []
    main = []
    for ex in out.get("main_text", []):
        main.extend(_as_text(v) for v in apply_extractor(ex, steps, page_url, vars_))
    fields = {}
    for k, ex in (out.get("fields") or {}).items():
        v = apply_extractor(ex, steps, page_url, vars_)
        if v: fields[k] = _as_text(v[0])
    links = []
    for ex in out.get("links", []):
        links.extend(_as_text(v) for v in apply_extractor(ex, steps, page_url, vars_))
    record = {"title": _as_text(t[0]) if t else "", "main_text": "\n".join(x for x in main if x.strip()),
              "fields": fields, "links": list(dict.fromkeys(links))}
    if not record["main_text"].strip() and not record["links"]:
        raise RecipeError("empty extraction")
    return record, info


def live_transport(http):
    async def tx(method, url, headers, body):
        r = await http.request(method, url, headers=headers, content=body.encode() if isinstance(body, str) else body)
        return r.status_code, r.text
    return tx


def _key(method, url, body):
    from urllib.parse import urlsplit, parse_qsl, urlencode
    s = urlsplit(url)
    q = urlencode(sorted(parse_qsl(s.query, keep_blank_values=True)))
    b = None
    if body:
        try: b = json.dumps(json.loads(body), sort_keys=True)
        except Exception: b = body
    return (method.upper(), f"{s.netloc}{s.path}?{q}", b)


def offline_transport(capture):
    """Serve requests from a stored render capture (exact match on method+URL(+JSON body))."""
    idx = {}
    for r in capture.get("requests", []):
        if r.get("status") is not None and r.get("body") is not None:
            idx.setdefault(_key(r["method"], r["url"], r.get("post_data")), (r["status"], r["body"]))
            idx.setdefault(_key(r["method"], r["url"], None) + ("nobody",), (r["status"], r["body"]))
    async def tx(method, url, headers, body):
        k = _key(method, url, body)
        if k in idx: return idx[k]
        k2 = _key(method, url, None) + ("nobody",)
        if k2 in idx and not body: return idx[k2]
        return None, ""
    return tx
