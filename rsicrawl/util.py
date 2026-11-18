"""Shared utilities: URL patterns, token F1, robots, per-host rate budget, budgeted HTTP client."""
import asyncio, re, time, urllib.robotparser
from collections import defaultdict
from urllib.parse import urlsplit, parse_qsl, urljoin
import httpx

from .config import USER_AGENT as UA
_SSL = httpx.create_ssl_context()  # shared: building an SSL context costs ~16 ms CPU per client

# ---------------------------------------------------------------- text / scoring
_tok = re.compile(r"[a-z0-9]+")


def tokens(s):
    return _tok.findall((s or "").lower())


def token_f1(pred, ref):
    from collections import Counter
    p, r = Counter(tokens(pred)), Counter(tokens(ref))
    if not p and not r: return 1.0
    if not p or not r: return 0.0
    common = sum((p & r).values())
    if common == 0: return 0.0
    prec, rec = common / sum(p.values()), common / sum(r.values())
    return 2 * prec * rec / (prec + rec)


def norm_url(u):
    s = urlsplit(u)
    path = s.path or "/"
    return f"{s.scheme}://{s.netloc}{path}" + (f"?{s.query}" if s.query else "")


def link_recall(pred, ref):
    ref = {norm_url(x) for x in ref}
    if not ref: return 1.0
    pred = {norm_url(x) for x in pred}
    return len(ref & pred) / len(ref)


def norm_title(t):
    return " ".join(tokens(t))


def equivalent(rec, ref, f1_min=0.9, link_min=0.95):
    """Equivalence test of the main design §6.5. Returns (ok, detail)."""
    f1 = token_f1(rec.get("main_text", ""), ref.get("main_text", ""))
    lr = link_recall(rec.get("links", []), ref.get("links", []))
    tt = norm_title(rec.get("title", "")) == norm_title(ref.get("title", "")) or not ref.get("title")
    fields_ok = all(str(v).strip() == str(ref.get("fields", {}).get(k, v)).strip() for k, v in (rec.get("fields") or {}).items())
    ok = f1 >= f1_min and lr >= link_min and tt and fields_ok
    return ok, {"f1": round(f1, 3), "link_recall": round(lr, 3), "title_ok": tt, "fields_ok": fields_ok}

# ---------------------------------------------------------------- URL patterns


def _seg_kind(seg):
    if seg.isdigit(): return "{num}"
    if "-" in seg or "_" in seg or len(seg) > 14 or re.search(r"\d", seg): return "{str}"
    return seg


def url_pattern(url):
    """Generic URL template: numeric segments -> {num}, slug-like -> {str}; query values abstracted."""
    s = urlsplit(url)
    segs = [p for p in s.path.split("/")]
    tpl = "/".join(_seg_kind(p) if p else p for p in segs)
    if s.query:
        q = "&".join(f"{k}={_seg_kind(v) if v else ''}" for k, v in sorted(parse_qsl(s.query, keep_blank_values=True)))
        tpl += "?" + q
    return f"{s.netloc}{tpl}"


def url_vars(url):
    """Variables a request template may reference, derived from the page URL alone."""
    s = urlsplit(url)
    v = {"origin": f"{s.scheme}://{s.netloc}", "page_url": url, "path": s.path, "path_noslash": s.path.strip("/"),
         "query": s.query}
    segs = [p for p in s.path.split("/") if p]
    for i, seg in enumerate(segs):
        v[f"seg{i}"] = seg
        m = re.match(r"^(.*?)[-_](\d+)$", seg)
        if m: v[f"seg{i}_head"], v[f"seg{i}_num"] = m.group(1), m.group(2)
        m = re.match(r"^(\d+)[-_](.*)$", seg)
        if m: v[f"seg{i}_lead"], v[f"seg{i}_tail"] = m.group(1), m.group(2)
    if segs: v["last"] = segs[-1]
    for k, val in parse_qsl(s.query): v[f"q_{k}"] = val
    return v


def fill(template, vars_):
    """Fill {name} placeholders; raise KeyError on missing variables."""
    if template is None: return None
    if not isinstance(template, str): return template
    def rep(m):
        k = m.group(1)
        if k not in vars_: raise KeyError(k)
        return str(vars_[k])
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", rep, template)

# ---------------------------------------------------------------- politeness


class HostBudget:
    """Shared per-host request budget (token bucket). Every request by every tier draws from it."""
    def __init__(self, rate_per_s=2.0, burst=4):
        self.rate, self.burst = rate_per_s, burst
        self.tokens = defaultdict(lambda: float(burst)); self.ts = defaultdict(time.monotonic)
        self.locks = defaultdict(asyncio.Lock)
        self.used = defaultdict(int); self.learning_used = defaultdict(int)

    def _refill(self, host):
        now = time.monotonic(); dt = now - self.ts[host]; self.ts[host] = now
        self.tokens[host] = min(self.burst, self.tokens[host] + dt * self.rate)

    async def acquire(self, host, n=1, learning=False):
        """Wait until n tokens are available (may go into debt for n > burst). Returns wait seconds."""
        t0 = time.monotonic()
        async with self.locks[host]:
            self._refill(host)
            need = min(n, self.burst)
            while self.tokens[host] < need:
                await asyncio.sleep((need - self.tokens[host]) / self.rate)
                self._refill(host)
            self.tokens[host] -= n
        self.used[host] += n
        if learning: self.learning_used[host] += n
        return time.monotonic() - t0

    def charge(self, host, n, learning=False):
        """Record requests already made (e.g. browser sub-requests) as debt."""
        self.tokens[host] -= n; self.used[host] += n
        if learning: self.learning_used[host] += n


class Robots:
    def __init__(self):
        self.cache = {}

    async def load(self, origin, client):
        if origin in self.cache: return self.cache[origin]
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = await client.get(origin + "/robots.txt", headers={"User-Agent": UA}, timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        self.cache[origin] = rp
        return rp

    def allowed(self, url):
        s = urlsplit(url); rp = self.cache.get(f"{s.scheme}://{s.netloc}")
        return True if rp is None else rp.can_fetch(UA, url)


class BudgetedHTTP:
    """HTTP for fast tiers and agent experiments: robots-checked and rate-budgeted. One cookie jar per instance."""
    def __init__(self, budget, robots, learning=False, timeout=15, cookies=None):
        self.budget, self.robots, self.learning = budget, robots, learning
        self.client = httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA}, cookies=cookies, verify=_SSL)
        self.requests = 0; self.bytes = 0; self.wait_s = 0.0

    async def request(self, method, url, headers=None, content=None):
        s = urlsplit(url)
        await self.robots.load(f"{s.scheme}://{s.netloc}", self.client)
        if not self.robots.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        self.wait_s += await self.budget.acquire(s.netloc, 1, learning=self.learning)
        r = await self.client.request(method, url, headers=headers or {}, content=content)
        self.requests += 1; self.bytes += len(r.content)
        return r

    async def aclose(self):
        await self.client.aclose()
