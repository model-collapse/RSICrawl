import json, re
from urllib.parse import urlsplit, unquote, parse_qsl

SKIP_VARS = ("origin", "page_url", "path", "path_noslash", "query")
STD_HEADERS = {"accept", "user-agent", "cookie", "content-length", "host", "connection",
               "accept-encoding", "accept-language", "origin", "sec-fetch-mode", "sec-fetch-site",
               "sec-fetch-dest", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "content-type"}


def jparse(b):
    if not isinstance(b, str):
        return None
    b = b.strip()
    if not b or b[0] not in "[{":
        return None
    try:
        return json.loads(b)
    except Exception:
        return None


def leaves(o, p=""):
    if isinstance(o, dict):
        for k, v in o.items():
            if not isinstance(k, str) or "." in k or "[" in k:
                continue
            yield from leaves(v, (p + "." + k) if p else k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from leaves(v, p + "[%d]" % i)
    else:
        yield p, o


def nodes(o, p=""):
    yield p, o
    if isinstance(o, dict):
        for k, v in o.items():
            if not isinstance(k, str) or "." in k or "[" in k:
                continue
            yield from nodes(v, (p + "." + k) if p else k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from nodes(v, p + "[%d]" % i)


def gen(p):
    return re.sub(r"\[\d+\]", "[*]", p)


def sval(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v
    return None


def getpath(o, p):
    cur = [o]
    if not p:
        return cur
    for part in re.findall(r"[^.\[\]]+|\[\*\]|\[\d+\]", p):
        nxt = []
        for c in cur:
            if part == "[*]":
                if isinstance(c, list):
                    nxt.extend(c)
            elif part.startswith("["):
                i = int(part[1:-1])
                if isinstance(c, list) and i < len(c):
                    nxt.append(c[i])
            else:
                if isinstance(c, dict) and part in c:
                    nxt.append(c[part])
        cur = nxt
    return cur


def shape(r):
    u = urlsplit(r.get("url", ""))
    segs = [s for s in u.path.split("/") if s]
    keys = tuple(sorted(k for k, _ in parse_qsl(u.query, keep_blank_values=True)))
    return (r.get("method", "GET"), u.netloc, len(segs), segs[0] if segs else "", keys)


class Ctx:
    def __init__(self, ev):
        self.ev = ev
        self.steps = {}
        self.names = {}
        self.varkeys = {}
        self.nv = 0
        self.fail = False
        self.steps[0] = {"name": "page", "method": "GET", "url": "{{page_url}}", "parse": "html", "extract": {}}

    def align(self, j, s):
        if s == 0:
            return j
        r0 = self.ev[0]["requests"]
        sh = shape(r0[j])
        ordn = sum(1 for k in range(j) if shape(r0[k]) == sh)
        cnt = 0
        for k, r in enumerate(self.ev[s]["requests"]):
            if shape(r) == sh:
                if cnt == ordn:
                    return k
                cnt += 1
        return None

    def addvar(self, j, extractor):
        key = (j, json.dumps(extractor, sort_keys=True))
        if key in self.varkeys:
            return self.varkeys[key]
        self.nv += 1
        name = "v%d" % self.nv
        self.varkeys[key] = name
        self.steps[j]["extract"][name] = extractor
        return name

    def html_source(self, vals, si_idx):
        htmls = [smp["requests"][0].get("body") or "" for smp in self.ev]
        h0 = htmls[0]
        t0 = vals[0]
        m = re.search(r'<meta[^>]*name=["\']([^"\']+)["\'][^>]*content=["\']' + re.escape(t0) + '["\']', h0)
        if m:
            nm = m.group(1)
            ok = True
            for h, t in zip(htmls, vals):
                mm = re.search(r'<meta[^>]*name=["\']' + re.escape(nm) + r'["\'][^>]*content=["\']([^"\']*)["\']', h)
                if not mm or mm.group(1) != t:
                    ok = False
            if ok:
                return {"css": "meta[name=%s]" % nm, "attr": "content", "first": True}
        if len(t0) < 3:
            return None
        if re.fullmatch(r"[A-Za-z0-9_\-]+", t0):
            cls = r"([A-Za-z0-9_\-]+)"
        elif re.fullmatch(r"[A-Za-z0-9_\-.~%+=/]+", t0):
            cls = r"([A-Za-z0-9_\-.~%+=/]+)"
        else:
            cls = r"([^\"'<>]+?)"
        for i in [m.start() for m in re.finditer(re.escape(t0), h0)][:5]:
            for L in (40, 30, 20, 14, 10, 6):
                if i - L < 0:
                    continue
                pre = h0[i - L:i]
                if "\n" in pre:
                    pre = pre.split("\n")[-1]
                    if len(pre) < 4:
                        continue
                post = h0[i + len(t0):i + len(t0) + 1] if cls.endswith("+?)") else ""
                rx = re.escape(pre) + cls + re.escape(post)
                ok = True
                for h, t in zip(htmls, vals):
                    try:
                        mm = re.search(rx, h)
                    except Exception:
                        ok = False
                        break
                    if not mm or mm.group(1) != t:
                        ok = False
                        break
                if ok:
                    return {"regex": rx, "first": True}
        return None

    def find_source(self, vals, before):
        names = list(self.ev[0].get("vars", {}).keys())
        names.sort(key=lambda n: (n != "last", n))
        for n in names:
            if n in SKIP_VARS:
                continue
            if all(str(smp.get("vars", {}).get(n)) == v for smp, v in zip(self.ev, vals)):
                return "{{%s}}" % n
        ex = self.html_source(vals, 0)
        if ex is not None:
            return "{{%s}}" % self.addvar(0, ex)
        r0 = self.ev[0]["requests"]
        for j in range(before[0] - 1, 0, -1):
            o = jparse(r0[j].get("body"))
            if o is None:
                continue
            for p, v in leaves(o):
                if sval(v) != vals[0]:
                    continue
                ok = True
                for s, smp in enumerate(self.ev):
                    k = self.align(j, s)
                    if k is None or k >= before[s]:
                        ok = False
                        break
                    oo = jparse(smp["requests"][k].get("body"))
                    g = getpath(oo, p) if oo is not None else []
                    if not g or sval(g[0]) != vals[s]:
                        ok = False
                        break
                if ok:
                    if not self.ensure_step(j):
                        continue
                    return "{{%s}}" % self.addvar(j, {"path": p, "first": True})
        return None

    def ensure_step(self, j):
        if j in self.steps:
            return True
        idxs = [self.align(j, s) for s in range(len(self.ev))]
        if any(i is None for i in idxs):
            return False
        reqs = [smp["requests"][i] for smp, i in zip(self.ev, idxs)]
        url = self.url_template([r["url"] for r in reqs], idxs)
        if url is None:
            return False
        hdrs = self.headers(reqs, idxs)
        if hdrs is None:
            return False
        body = None
        bodies = [r.get("post_data") for r in reqs]
        if bodies[0] is not None:
            if all(b == bodies[0] for b in bodies):
                body = bodies[0]
            else:
                return False
        self.steps[j] = {"name": "s%d" % j, "method": reqs[0].get("method", "GET"), "url": url,
                         "headers": hdrs, "body": body, "parse": "json", "extract": {}}
        return True

    def token(self, toks, idxs):
        if all(t == toks[0] for t in toks):
            return toks[0]
        vals = [unquote(t) for t in toks]
        src = self.find_source(vals, idxs)
        if src:
            return src
        names = sorted([n for n in self.ev[0].get("vars", {}) if n not in SKIP_VARS],
                       key=lambda n: (n != "last", n))
        for n in names:
            vs = [str(smp["vars"].get(n, "")) for smp in self.ev]
            if not all(v and v in t for v, t in zip(vs, toks)):
                continue
            res = [t.replace(v, "\x00") for v, t in zip(vs, toks)]
            if all(r == res[0] for r in res):
                return res[0].replace("\x00", "{{%s}}" % n)
        return None

    def url_template(self, urls, idxs):
        parts = [urlsplit(u) for u in urls]
        origins = [smp["vars"].get("origin") for smp in self.ev]
        if all("%s://%s" % (p.scheme, p.netloc) == o for p, o in zip(parts, origins)):
            base = "{{origin}}"
        elif all(p.netloc == parts[0].netloc for p in parts):
            base = "%s://%s" % (parts[0].scheme, parts[0].netloc)
        else:
            return None
        segss = [p.path.split("/") for p in parts]
        if any(len(s) != len(segss[0]) for s in segss):
            return None
        out = []
        for i in range(len(segss[0])):
            t = self.token([s[i] for s in segss], idxs)
            if t is None:
                return None
            out.append(t)
        url = base + "/".join(out)
        qs = [p.query.split("&") if p.query else [] for p in parts]
        if any(len(q) != len(qs[0]) for q in qs):
            return None
        qo = []
        for i in range(len(qs[0])):
            kvs = [q[i].split("=", 1) for q in qs]
            if any(kv[0] != kvs[0][0] for kv in kvs):
                return None
            if len(kvs[0]) == 1:
                qo.append(kvs[0][0])
                continue
            t = self.token([kv[1] if len(kv) > 1 else "" for kv in kvs], idxs)
            if t is None:
                return None
            qo.append(kvs[0][0] + "=" + t)
        if qo:
            url += "?" + "&".join(qo)
        return url

    def headers(self, reqs, idxs):
        h = {"Accept": "application/json"}
        hs = [{k.lower(): v for k, v in (r.get("headers") or {}).items()} for r in reqs]
        for k in hs[0]:
            if k in STD_HEADERS or k.startswith(":"):
                continue
            vals = [x.get(k) for x in hs]
            if any(v is None for v in vals):
                continue
            if k == "referer":
                if all(v == smp["vars"].get("page_url") for v, smp in zip(vals, self.ev)):
                    h["Referer"] = "{{page_url}}"
                continue
            if all(v == vals[0] for v in vals):
                h[k] = vals[0]
                continue
            src = self.find_source(vals, idxs)
            if src is None:
                return None
            h[k] = src
        return h


def score_target(o, ref):
    lines = [l.strip() for l in (ref.get("main_text") or "").split("\n") if l.strip()]
    strs = set()
    for p, v in leaves(o):
        sv = sval(v)
        if sv:
            strs.add(sv.strip())
            for l in re.split(r"\n+", sv):
                strs.add(l.strip())
    sc = sum(1 for l in lines if l in strs)
    t = (ref.get("title") or "").strip()
    return sc + (5 if t and t in strs else 0)


def build_output(o, ref, sname, origin):
    out = {}
    lv = list(leaves(o))
    t = (ref.get("title") or "").strip()
    tpath = None
    for p, v in lv:
        if isinstance(v, str) and v.strip() == t and t:
            tpath = p
            break
    if tpath is None:
        return None
    out["title"] = {"step": sname, "path": tpath}
    lines = [l.strip() for l in (ref.get("main_text") or "").split("\n") if l.strip()]
    seq = []
    for l in lines:
        found = None
        for p, v in lv:
            if isinstance(v, str) and v.strip() == l:
                found = ("p", gen(p)); break
        if found is None:
            for p, v in lv:
                if isinstance(v, str) and "\n" in v and l in [x.strip() for x in re.split(r"\n+", v)]:
                    found = ("s", gen(p)); break
        if found is None:
            m = re.match(r"^([^:]{1,60}):\s*(.*)$", l)
            if m:
                k, val = m.group(1).strip(), m.group(2).strip()
                for p, n in nodes(o):
                    if isinstance(n, dict) and k in n and sval(n[k]) is not None and sval(n[k]).strip() == val:
                        found = ("kv", gen(p), k); break
        if found is None:
            for p, v in lv:
                sv = sval(v)
                if sv is not None and sv.strip() == l:
                    found = ("p", gen(p)); break
        if found:
            seq.append(found)
    mt = []
    i = 0
    while i < len(seq):
        f = seq[i]
        if f[0] == "kv":
            keys = [f[2]]
            j = i + 1
            while j < len(seq) and seq[j][0] == "kv" and seq[j][1] == f[1]:
                if seq[j][2] not in keys:
                    keys.append(seq[j][2])
                j += 1
            d = getpath(o, f[1])
            d = d[0] if d else {}
            if isinstance(d, dict) and set(keys) == set(d.keys()):
                mt.append({"step": sname, "path": f[1], "kv": True})
            else:
                mt.append({"step": sname, "path": f[1],
                           "template": "\n".join("%s: {{%s}}" % (k, k) for k in keys)})
            i = j
            continue
        j = i + 1
        while j < len(seq) and seq[j] == f:
            j += 1
        e = {"step": sname, "path": f[1]}
        if f[0] == "s":
            e["split"] = "\n+"
        if not (mt and mt[-1] == e):
            mt.append(e)
        i = j
    out["main_text"] = mt
    links = ref.get("links") or []
    lks = []
    if links:
        lset = set(links)
        best = None
        for p, n in nodes(o):
            if not isinstance(n, list) or not n:
                continue
            if all(isinstance(x, dict) for x in n):
                for k in n[0].keys():
                    vs = [sval(x.get(k)) for x in n]
                    if any(v is None or v == "" for v in vs):
                        continue
                    pres = set()
                    okc = 0
                    for v in vs:
                        for L in links:
                            if L.endswith(v):
                                pres.add(L[:-len(v)]); okc += 1; break
                    if okc == len(vs) and len(pres) == 1:
                        pre = pres.pop()
                        tpl = pre.replace(origin, "{{origin}}", 1) if pre.startswith(origin) else pre
                        cand = {"step": sname, "path": gen(p) + "[*]", "template": tpl + "{{%s}}" % k}
                        if tpl.startswith("/"):
                            cand["abs"] = True
                        sc = (okc, -len(pre))
                        if best is None or sc > best[0]:
                            best = (sc, cand)
            elif all(isinstance(x, str) for x in n):
                okc = sum(1 for x in n if x in lset or (origin + x) in lset)
                if okc == len(n):
                    cand = {"step": sname, "path": gen(p) + "[*]", "abs": True}
                    sc = (okc, 0)
                    if best is None or sc > best[0]:
                        best = (sc, cand)
        if best:
            lks.append(best[1])
    out["links"] = lks
    return out


def generate(evidence):
    ev = evidence
    if not ev:
        return []
    ref = ev[0].get("ref") or {}
    r0 = ev[0]["requests"]
    best = None
    for j in range(1, len(r0)):
        if r0[j].get("status", 200) != 200:
            continue
        o = jparse(r0[j].get("body"))
        if o is None:
            continue
        sc = score_target(o, ref)
        if sc >= 5 and (best is None or sc > best[0]):
            best = (sc, j)
    if best is None:
        return []
    tj = best[1]
    recipes = []
    for referer in (False, True):
        ctx = Ctx(ev)
        if not ctx.ensure_step(tj):
            return []
        st = ctx.steps[tj]
        if referer:
            st["headers"]["Referer"] = "{{page_url}}"
        tname = st["name"]
        o = jparse(r0[tj]["body"])
        out = build_output(o, ref, tname, ev[0]["vars"].get("origin", ""))
        if out is None:
            return []
        steps = [ctx.steps[k] for k in sorted(ctx.steps)]
        if not steps[0]["extract"]:
            steps = steps[1:]
        for s in steps:
            if s.get("body") is None:
                s.pop("body", None)
        recipes.append({"id": "chain-%d" % (1 + referer), "pattern_id": "", "tier": "T0",
                        "origin": "skill:json-api-chain", "steps": steps, "output": out})
    return recipes
