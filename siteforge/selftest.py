"""SiteForge self-test: generate seed 7, serve it, render pages in Lightpanda, compare to truth.

    python -m siteforge.selftest [--no-drift]
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from .generate import generate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORGE = os.path.join(ROOT, "runs", "forge7")
from rsicrawl.config import lightpanda_bin
LIGHTPANDA = lightpanda_bin()
CONTROL = 18999
CTRL = f"http://127.0.0.1:{CONTROL}"
SEM = asyncio.Semaphore(8)
ROWS: list[tuple] = []


def row(site, check, ok, detail=""):
    ROWS.append((site["site_id"], site["family"], site["archetype"], check,
                 "PASS" if ok else "FAIL", detail))


async def render(url: str) -> str:
    async with SEM:
        env = dict(os.environ, LIGHTPANDA_DISABLE_TELEMETRY="true")
        p = await asyncio.create_subprocess_exec(
            LIGHTPANDA, "fetch", "--log-level", "fatal", "--wait-until", "networkidle",
            "--dump", "html", url, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), 60)
        except asyncio.TimeoutError:
            p.kill()
            return ""
        return out.decode("utf-8", "replace")


def tokens(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def path_of(site, url):
    return url.split(str(site["port"]), 1)[1]


def extract(html: str, url: str) -> dict | None:
    """Generic (class-agnostic) main-content extraction, so it also works after D1."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main", id="content")
    if main is None or main.find("h1") is None:
        return None
    return {
        "doc_title": soup.title.get_text() if soup.title else "",
        "title": main.find("h1").get_text(),
        "paras": [p.get_text() for p in main.find_all("p")],
        "lis": [li.get_text() for li in main.find_all("li")],
        "links": [urljoin(url, a.get("href", "")) for a in main.find_all("a")],
        "strict_ok": bool(main.select("div.body p") and main.select("ul.specs li")
                          and main.select("div.related a")),
    }


def compare_item(got: dict | None, truth: dict, site_name: str, strict=True) -> str:
    if got is None:
        return "no main/h1"
    errs = []
    if got["title"] != truth["title"]:
        errs.append(f"title {got['title']!r}")
    if got["doc_title"] != f"{truth['title']} | {site_name}":
        errs.append(f"doc_title {got['doc_title']!r}")
    specs = dict(li.split(": ", 1) for li in got["lis"] if ": " in li)
    if specs != truth["fields"]:
        errs.append(f"specs {specs}")
    if got["links"] != truth["links"]:
        errs.append("links differ")
    want_body = tokens(" ".join(truth["main_text"].split("\n")[1:-4]))
    if tokens(" ".join(got["paras"])) != want_body:
        errs.append("body tokens differ")
    if strict and not got["strict_ok"]:
        errs.append("class structure (div.body/ul.specs/div.related) missing")
    return "; ".join(errs)


def compare_list(got: dict | None, truth: dict) -> str:
    if got is None:
        return "no main/h1"
    errs = []
    if got["title"] != truth["title"]:
        errs.append("title")
    if got["links"] != truth["links"]:
        errs.append("links")
    if "\n".join([got["title"]] + got["lis"]) != truth["main_text"]:
        errs.append("main_text")
    return "; ".join(errs)


async def check_item_render(site, truth, label="render", strict=True):
    html = await render(truth["url"])
    err = compare_item(extract(html, truth["url"]), truth, site["name"], strict)
    row(site, f"{label} {path_of(site, truth['url'])[:34]}", not err, err)
    return not err


async def http(sess, method, url, **kw):
    async with sess.request(method, url, **kw) as r:
        return r.status, await r.text()


async def test_site(sess, site, truths):
    arch, fam = site["archetype"], site["family"]
    items = [t for t in truths if t["pattern"] == "item"][:2]
    lists = [t for t in truths if t["pattern"] == "list"][:1]
    base = f"http://127.0.0.1:{site['port']}"
    if arch == "A11":
        for t in items:
            st, body = await http(sess, "GET", t["url"])
            html = await render(t["url"])
            ok = st == 403 and "Checking your browser" in html and t["blocked"]
            row(site, "blocked 403 " + path_of(site, t["url"])[:28], ok, f"status={st}")
        st1, _ = await http(sess, "GET", base + "/list?page=1")
        st2, _ = await http(sess, "GET", base + "/")
        row(site, "list + home 403", (st1, st2) == (403, 403), f"{st1}/{st2}")
        st, _ = await http(sess, "GET", base + "/robots.txt")
        row(site, "robots 200", st == 200, f"status={st}")
        return
    await asyncio.gather(*(check_item_render(site, t) for t in items))
    for t in lists:
        html = await render(t["url"])
        err = compare_list(extract(html, t["url"]), t)
        row(site, "list render " + path_of(site, t["url"]), not err, err)
    st, robots = await http(sess, "GET", base + "/robots.txt")
    want = "Disallow: /api/" in robots
    row(site, "robots.txt", st == 200 and want == (arch == "A12"), robots.strip().replace("\n", " | "))
    t = items[0]
    st, body = await http(sess, "GET", t["url"])
    if arch == "A2":
        marker = {"next": '<script id="__NEXT_DATA__" type="application/json">',
                  "nuxt": "window.__NUXT__=", "wp": '<script type="application/ld+json">'}[fam]
        ok = st == 200 and marker in body and json.dumps(t["title"])[1:-1] in body.split(marker, 1)[1]
        row(site, "plain GET has embedded JSON", ok, marker[:30])
    elif arch == "A1":
        row(site, "plain GET is SSR", st == 200 and f"<h1>{t['title']}</h1>" in body)
    else:
        main = re.search(r'<main id="content">(.*?)</main>', body, re.S)
        row(site, "plain GET shell has empty main", bool(st == 200 and main and not main.group(1).strip()))
    if arch == "A5":
        iid = t["url"].rsplit("/", 1)[1]
        st, _ = await http(sess, "GET", f"{base}/api/v1/items/{iid}")
        tok = re.search(r'name="csrf-token" content="([^"]+)"', body).group(1)
        st2, _ = await http(sess, "GET", f"{base}/api/v1/items/{iid}", headers={"X-CSRF-Token": tok})
        row(site, "csrf: 403 without, 200 with", st == 403 and st2 == 200, f"{st}/{st2}")
    if arch == "A6":
        iid = t["url"].rsplit("/", 1)[1]
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s2:
            st, _ = await http(s2, "GET", f"{base}/api/v1/items/{iid}")
            await http(s2, "GET", f"{base}/api/session")
            st2, _ = await http(s2, "GET", f"{base}/api/v1/items/{iid}")
        row(site, "session: 401 without, 200 with", st == 401 and st2 == 200, f"{st}/{st2}")
    if arch == "A10":
        iid = t["url"].rsplit("/", 1)[1]
        secret = re.search(r'var SECRET="([^"]+)"', body).group(1)
        now = int(time.time())

        def sig(ts):
            return hashlib.sha256(f"{secret}{iid}{ts}".encode()).hexdigest()
        st1, _ = await http(sess, "GET", f"{base}/api/v1/items/{iid}?ts={now}&sig={sig(now)}")
        st2, _ = await http(sess, "GET", f"{base}/api/v1/items/{iid}?ts={now - 60}&sig={sig(now - 60)}")
        st3, _ = await http(sess, "GET", f"{base}/api/v1/items/{iid}?ts={now}&sig={'0' * 64}")
        row(site, "sig: fresh 200, stale 403, bad 403", (st1, st2, st3) == (200, 403, 403),
            f"{st1}/{st2}/{st3}")


async def drift(sess, sid, ev):
    async with sess.post(CTRL + "/drift", json={"site_id": sid, "event": ev}) as r:
        return (await r.json())["applied"]


async def drift_tests(sess, sites, truths):
    """Apply drift events and verify the browser-rendered content still matches truth."""
    def pick(fam, arch):
        return next(s for s in sites if s["family"] == fam and s["archetype"] == arch)

    def item_truth(s, k=0):
        return [t for t in truths[s["site_id"]] if t["pattern"] == "item"][k]

    plans = [(pick("next", "A1"), ["D1", "D6"]), (pick("next", "A2"), ["D3", "D5"]),
             (pick("nuxt", "A2"), ["D3", "D6", "D2"]), (pick("wp", "A2"), ["D6", "D3"]),
             (pick("wp", "A3"), ["D2", "D3", "D4"]), (pick("spa", "A3"), ["D2", "D4", "D1"]),
             (pick("gql", "A4"), ["D2", "D3", "D4"]), (pick("spa", "A6"), ["D3", "D4"]),
             (pick("spa", "A7"), ["D2", "D3"]), (pick("spa", "A10"), ["D2", "D3"]),
             (pick("next", "A3"), ["D5", "D3", "D4"]), (pick("spa", "A5"), ["D2", "D3"])]

    s = pick("next", "A2")
    async with sess.get(CTRL + "/state") as r:
        st0 = await r.json()
    with open(os.path.join(FORGE, "server_data", f"{s['site_id']}.json")) as f:
        sd = json.load(f)
    t = item_truth(s)
    iid = t["url"].split("/items/")[1].split("/")[0]
    old_bid = sd["build_ids"][st0[s["site_id"]]["build_idx"]]
    old_url = f"http://127.0.0.1:{s['port']}/_next/data/{old_bid}/items/{iid}.json"
    stA, _ = await http(sess, "GET", old_url)
    spa = pick("spa", "A3")
    spa_old = f"http://127.0.0.1:{spa['port']}/api/v1/items/{item_truth(spa)['url'].rsplit('/', 1)[1]}"
    stB, _ = await http(sess, "GET", spa_old)

    for site, evs in plans:
        applied = [await drift(sess, site["site_id"], ev) for ev in evs]
        row(site, f"drift {'+'.join(evs)} applied", all(applied), str(applied))
    stA2, _ = await http(sess, "GET", old_url)
    row(s, "D5: old buildId 200 -> 404", (stA, stA2) == (200, 404), f"{stA}->{stA2}")
    stB2, _ = await http(sess, "GET", spa_old)
    row(spa, "D2: old /api/v1 200 -> 404", (stB, stB2) == (200, 404), f"{stB}->{stB2}")
    _, body = await http(sess, "GET", t["url"])
    row(s, "D3: __NEXT_DATA__ field renamed", '"headline"' in body)
    na = [(pick("spa", "A11"), "D1"), (pick("spa", "A3"), "D5"), (pick("spa", "A3"), "D6"),
          (pick("next", "A1"), "D2"), (pick("nuxt", "A1"), "D4"), (pick("spa", "A5"), "D4")]
    for site, ev in na:
        a = await drift(sess, site["site_id"], ev)
        row(site, f"drift {ev} n/a -> applied=false", a is False, str(a))

    await asyncio.gather(*(check_item_render(site, item_truth(site, 1), "post-drift render", strict=False)
                           for site, _ in plans))

    async with sess.get(CTRL + "/log", params={"site_id": spa["site_id"]}) as r:
        log = await r.json()
    ok = log and all(set(e) == {"t", "method", "path", "status"} for e in log) \
        and any(e["status"] == 404 and e["path"].startswith("/api/v1/") for e in log)
    row(spa, "control /log shape + contents", bool(ok), f"{len(log)} entries")
    async with sess.post(CTRL + "/reset") as r:
        await r.json()
    stB3, _ = await http(sess, "GET", spa_old)
    row(spa, "reset restores /api/v1", stB3 == 200, str(stB3))


async def amain(do_drift: bool):
    sites = generate(7, FORGE)
    truths = {}
    for s in sites:
        with open(os.path.join(FORGE, "truth", f"{s['site_id']}.jsonl")) as f:
            truths[s["site_id"]] = [json.loads(line) for line in f]
    proc = subprocess.Popen([sys.executable, "-m", "siteforge.server", "--forge", FORGE,
                             "--control-port", str(CONTROL)], cwd=ROOT)
    try:
        async with aiohttp.ClientSession() as sess:
            for _ in range(100):
                try:
                    async with sess.get(CTRL + "/health") as r:
                        if r.status == 200:
                            break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.1)
            else:
                raise SystemExit("server did not start")
            t0 = time.time()
            await asyncio.gather(*(test_site(sess, s, truths[s["site_id"]]) for s in sites))
            if do_drift:
                await drift_tests(sess, sites, truths)
            dt = time.time() - t0
    finally:
        proc.terminate()
        proc.wait(10)
    late = ("drift", "D", "post", "control", "reset")
    ROWS.sort(key=lambda r: (r[0], r[3].startswith(late)))
    w = max(len(r[3]) for r in ROWS)
    print(f"{'site':4} {'fam':4} {'arch':4} {'check':{w}} result  detail")
    for r in ROWS:
        print(f"{r[0]:4} {r[1]:4} {r[2]:4} {r[3]:{w}} {r[4]:6}  {r[5][:90]}")
    n_fail = sum(r[4] == "FAIL" for r in ROWS)
    print(f"\n{len(ROWS) - n_fail}/{len(ROWS)} checks passed in {dt:.1f}s")
    return n_fail


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-drift", action="store_true")
    a = ap.parse_args()
    sys.exit(1 if asyncio.run(amain(not a.no_drift)) else 0)


if __name__ == "__main__":
    main()
