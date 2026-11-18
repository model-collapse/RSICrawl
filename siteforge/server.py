"""Serve every SiteForge site on its own port plus a control app.

    python -m siteforge.server --forge runs/forge7 --control-port 18999
"""
import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import signal
import time

from aiohttp import web

from . import pages, shapes
from .common import item_path

HTML = "text/html"


def jresp(obj, status=200, **kw):
    return web.json_response(obj, status=status, **kw)


class Site:
    def __init__(self, data: dict):
        self.spec = data["spec"]
        self.data = data
        self.items = data["items"]
        self.by_id = {it["id"]: it for it in self.items}
        self.by_slug = {it["slug"]: it for it in self.items}
        self.by_post = {it["post_id"]: it for it in self.items}
        self.by_internal = {it["internal_id"]: it for it in self.items}
        fam = self.spec["family"]
        self.by_path = {item_path(fam, it): it for it in self.items}
        self.reset()

    def reset(self):
        self.st = shapes.fresh_state(self.spec)
        self.log = []
        self.sids = set()
        self.sid_rng = random.Random(f"sid:{self.data['seed']}:{self.spec['site_id']}")

    def rel(self, it):
        return [self.by_id[r] for r in it["related"]]

    # ---- drift --------------------------------------------------------------------
    def drift(self, ev: str) -> bool:
        st, fam, arch = self.st, self.spec["family"], self.spec["archetype"]
        if arch == "A11":
            return False
        if ev == "D1":
            st["style"] = 1 - st["style"]
            return True
        if ev == "D2":
            if st["mode"] == "csr" and fam != "next":
                st["api_bump"] += 1
                return True
            return False
        if ev == "D3":
            if st["mode"] in ("embed", "csr") and not st["renamed"]:
                st["renamed"] = True
                return True
            return False
        if ev == "D4":
            if st["mode"] == "csr" and not st["csrf"]:
                st["csrf"] = True
                return True
            return False
        if ev == "D5":
            if fam == "next":
                st["build_idx"] = (st["build_idx"] + 1) % len(self.data["build_ids"])
                return True
            return False
        if ev == "D6":
            if st["mode"] in ("ssr", "embed") and fam in ("next", "nuxt", "wp"):
                st["mode"] = "csr"
                return True
            return False
        return False

    # ---- request handling ------------------------------------------------------------
    async def handle(self, request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(self.spec["latency_ms"] / 1000.0)
        try:
            resp = await self.dispatch(request)
        except web.HTTPException as e:
            resp = e
        except Exception as e:  # never crash the server
            resp = jresp({"error": f"internal: {e!r}"}, status=500)
        self.log.append({"t": time.time(), "method": request.method,
                         "path": request.path_qs, "status": resp.status})
        return resp

    def html(self, body, status=200):
        return web.Response(text=body, status=status, content_type=HTML)

    def not_found(self, api=False):
        if api:
            return jresp({"error": "not found"}, status=404)
        return self.html(pages.layout(self.spec, self.st, f"Not found | {self.spec['name']}",
                                      "<h1>Page not found</h1>"), 404)

    def check_csrf(self, request):
        if self.st["csrf"] and request.headers.get("X-CSRF-Token") != self.data["csrf_token"]:
            return jresp({"error": "invalid or missing CSRF token"}, status=403)
        return None

    async def dispatch(self, request: web.Request):
        spec, st, path, m = self.spec, self.st, request.path, request.method
        fam, arch = spec["family"], spec["archetype"]
        if path == "/robots.txt":
            body = "User-agent: *\n" + ("Disallow: /api/\n" if arch == "A12" else "Allow: /\n")
            return web.Response(text=body, content_type="text/plain")
        if arch == "A11":
            return self.html(pages.challenge_page(), 403)
        if path == "/favicon.ico":
            return web.Response(status=404)
        if path.startswith("/static/bundle-") and path.endswith(".js"):
            n = path[len("/static/bundle-"):-3]
            return web.Response(text=f"window.__bundle{n}=function(a){{return a+{n};}};", content_type="application/javascript")
        if path == "/t/collect":
            return web.Response(status=204)
        if path in ("/_svc/recs", "/_svc/reviews"):
            return jresp({"widget": path.rsplit("/", 1)[1], "items": []})

        # ---- HTML pages ----
        if m == "GET":
            if path == "/":
                return self.html(pages.home_page(spec, st))
            if path == "/about":
                return self.html(pages.about_page(spec, st))
            if path == "/list":
                try:
                    page = int(request.query.get("page", "1"))
                except ValueError:
                    return self.not_found()
                if not 1 <= page <= spec["n_list_pages"]:
                    return self.not_found()
                return self.html(pages.list_page(spec, st, self.items, page))
            it = self.by_path.get(path)
            if it is not None:
                if st["mode"] == "csr":
                    return self.html(pages.item_page_csr(spec, st, self.data, it, self.rel(it)))
                return self.html(pages.item_page_ssr(spec, st, self.data, it, self.rel(it)))

        # ---- APIs ----
        if fam == "next":
            mm = re.fullmatch(r"/_next/data/([^/]+)/items/(\d+)\.json", path)
            if mm and m == "GET":
                if mm.group(1) != self.data["build_ids"][st["build_idx"]]:
                    return self.not_found(api=True)
                return self.with_item(request, self.by_id.get(int(mm.group(2))),
                                      lambda it: {"pageProps": {"item": shapes.next_item(it, self.rel(it), st)},
                                                  "__N_SSP": True})
        if fam == "nuxt":
            mm = re.fullmatch(re.escape(shapes.nuxt_api(st)) + r"/(\d+)", path)
            if mm and m == "GET":
                return self.with_item(request, self.by_id.get(int(mm.group(1))),
                                      lambda it: shapes.nuxt_item(it, self.rel(it), st))
        if fam == "wp":
            mm = re.fullmatch(re.escape(shapes.wp_ns(st)) + r"/posts/(\d+)", path)
            if mm and m == "GET":
                return self.with_item(request, self.by_post.get(int(mm.group(1))),
                                      lambda it: shapes.wp_post(it, self.rel(it), st, spec["port"]))
        if fam == "gql" and path == shapes.gql_path(st):
            return await self.graphql(request)
        if fam == "spa" and arch == "A7":
            return self.a7(request)
        if fam == "spa":
            if path == "/api/session" and m == "GET" and arch == "A6":
                sid = "%032x" % self.sid_rng.getrandbits(128)
                self.sids.add(sid)
                r = jresp({"ok": True})
                r.set_cookie("sid", sid, path="/", httponly=True, samesite="Lax")
                return r
            mm = re.fullmatch(re.escape(shapes.spa_api(st)) + r"/items/(\d+)", path)
            if mm and m == "GET":
                if arch == "A6" and request.cookies.get("sid") not in self.sids:
                    return jresp({"error": "unauthorized: valid session required"}, status=401)
                if arch == "A10":
                    bad = self.check_sig(request, mm.group(1))
                    if bad:
                        return bad
                return self.with_item(request, self.by_id.get(int(mm.group(1))),
                                      lambda it: shapes.spa_item(it, self.rel(it), st))
        return self.not_found(api=path.startswith(("/api", "/svc", "/wp-json", "/_next", "/graphql")))

    def with_item(self, request, it, fn):
        bad = self.check_csrf(request)
        if bad:
            return bad
        if it is None:
            return self.not_found(api=True)
        return jresp(fn(it))

    def check_sig(self, request, iid):
        ts, sig = request.query.get("ts", ""), request.query.get("sig", "")
        try:
            tsv = int(ts)
        except ValueError:
            return jresp({"error": "missing or invalid ts"}, status=403)
        if abs(time.time() - tsv) > 30:
            return jresp({"error": "stale timestamp"}, status=403)
        want = hashlib.sha256((self.data["a10_secret"] + iid + ts).encode()).hexdigest()
        if sig.lower() != want:
            return jresp({"error": "bad signature"}, status=403)
        return None

    def a7(self, request):
        st, path = self.st, request.path
        if path == "/api/config":
            return jresp({"apiBase": shapes.a7_base(st), "features": {"records": True}})
        base = shapes.a7_base(st)
        if path == base + "/lookup":
            bad = self.check_csrf(request)
            if bad:
                return bad
            it = self.by_slug.get(request.query.get("slug", ""))
            if it is None:
                return self.not_found(api=True)
            return jresp({"internalId": it["internal_id"], "slug": it["slug"]})
        mm = re.fullmatch(re.escape(base) + r"/records/([\w-]+)", path)
        if mm:
            return self.with_item(request, self.by_internal.get(mm.group(1)),
                                  lambda it: shapes.a7_record(it, self.rel(it), st))
        return self.not_found(api=True)

    async def graphql(self, request):
        if request.method != "POST":
            return jresp({"errors": [{"message": "GET not supported; use POST"}]}, status=405)
        bad = self.check_csrf(request)
        if bad:
            return bad
        try:
            body = await request.json()
            query, variables = body.get("query", ""), body.get("variables") or {}
        except Exception:
            return jresp({"errors": [{"message": "invalid JSON body"}]}, status=400)
        if not isinstance(query, str) or "product" not in query:
            return jresp({"errors": [{"message": "unknown query; only product(slug) is supported"}]}, status=400)
        old, new = ("attributes", "specs") if self.st["renamed"] else ("specs", "attributes")
        if re.search(rf"\b{old}\b", query) and not re.search(rf"\b{new}\b", query):
            return jresp({"errors": [{"message": f'Cannot query field "{old}" on type "Product". '
                                                  f'Did you mean "{new}"?'}], "data": None})
        slug = variables.get("slug")
        if slug is None:
            mm = re.search(r'product\s*\(\s*slug\s*:\s*"([^"]*)"', query)
            slug = mm.group(1) if mm else None
        it = self.by_slug.get(slug or "")
        prod = shapes.gql_product(it, self.rel(it), self.st) if it else None
        return jresp({"data": {"product": prod}})


def make_site_app(site: Site) -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", site.handle)
    return app


def make_control_app(sites: dict[str, Site]) -> web.Application:
    async def drift(request):
        try:
            body = await request.json()
        except Exception:
            return jresp({"error": "invalid JSON"}, status=400)
        site = sites.get(body.get("site_id"))
        if site is None:
            return jresp({"error": "unknown site_id"}, status=404)
        ev = body.get("event")
        if ev not in {"D1", "D2", "D3", "D4", "D5", "D6"}:
            return jresp({"error": "unknown event"}, status=400)
        applied = site.drift(ev)
        return jresp({"applied": applied, "site_id": site.spec["site_id"], "event": ev,
                      "state": site.st})

    async def log(request):
        sid = request.query.get("site_id")
        since = float(request.query.get("since", "0") or 0)
        if sid is None:
            return jresp({k: [e for e in s.log if e["t"] >= since] for k, s in sites.items()})
        site = sites.get(sid)
        if site is None:
            return jresp({"error": "unknown site_id"}, status=404)
        return jresp([e for e in site.log if e["t"] >= since])

    async def reset(request):
        for s in sites.values():
            s.reset()
        return jresp({"ok": True})

    async def state(request):
        return jresp({k: s.st for k, s in sites.items()})

    async def health(request):
        return jresp({"ok": True, "sites": len(sites)})

    app = web.Application()
    app.router.add_post("/drift", drift)
    app.router.add_get("/log", log)
    app.router.add_post("/reset", reset)
    app.router.add_get("/state", state)
    app.router.add_get("/health", health)
    return app


async def serve(forge: str, control_port: int) -> None:
    with open(os.path.join(forge, "sites.json")) as f:
        specs = json.load(f)
    sites = {}
    for spec in specs:
        with open(os.path.join(forge, "server_data", f"{spec['site_id']}.json")) as f:
            sites[spec["site_id"]] = Site(json.load(f))
    runners = []
    for site in sites.values():
        r = web.AppRunner(make_site_app(site), access_log=None)
        await r.setup()
        await web.TCPSite(r, "127.0.0.1", site.spec["port"]).start()
        runners.append(r)
    r = web.AppRunner(make_control_app(sites), access_log=None)
    await r.setup()
    await web.TCPSite(r, "127.0.0.1", control_port).start()
    runners.append(r)
    print(f"siteforge: serving {len(sites)} sites on ports "
          f"{min(s.spec['port'] for s in sites.values())}-{max(s.spec['port'] for s in sites.values())}, "
          f"control on {control_port}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    for r in runners:
        await r.cleanup()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--forge", required=True)
    ap.add_argument("--control-port", type=int, default=18999)
    a = ap.parse_args()
    asyncio.run(serve(a.forge, a.control_port))


if __name__ == "__main__":
    main()
