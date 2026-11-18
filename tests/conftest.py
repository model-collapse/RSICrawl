import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def page(title, paras, specs, related, embed=None, csr=False):
    """Synthetic item page in a shared template; optional embedded JSON or client-rendered (empty main)."""
    main = "" if csr else (f"<article><h1>{title}</h1><div class='body'>" + "".join(f"<p>{p}</p>" for p in paras) +
                           "</div><ul class='specs'>" + "".join(f"<li>{k}: {v}</li>" for k, v in specs.items()) +
                           "</ul><div class='related'>" + "".join(f"<a href='/items/{i}'>r{i}</a>" for i in related) + "</div></article>")
    emb = f"<script id='state' type='application/json'>{json.dumps(embed)}</script>" if embed else ""
    return (f"<!doctype html><html><head><title>{title} | Shop</title>{emb}</head><body><header><a href='/'>Shop</a>"
            f"<nav><a href='/about'>About</a></nav></header><main id='content'>{main}</main><footer><p>(c) Shop</p></footer></body></html>")


ITEMS = [
    {"id": 11, "title": "Red Kettle", "body": ["Boils water fast.", "Steel body with handle."], "specs": {"price": "19.99", "sku": "K-11"}, "related": [12, 13]},
    {"id": 12, "title": "Blue Mug", "body": ["Holds coffee.", "Ceramic and sturdy."], "specs": {"price": "7.50", "sku": "M-12"}, "related": [11, 14]},
    {"id": 13, "title": "Green Teapot", "body": ["Brews tea gently.", "Glass lid included."], "specs": {"price": "24.00", "sku": "T-13"}, "related": [11, 12]},
    {"id": 14, "title": "Black Pan", "body": ["Non-stick surface.", "Oven safe to 250C."], "specs": {"price": "31.00", "sku": "P-14"}, "related": [13, 12]},
]
ORIGIN = "http://shop.test"


def capture(it, mode):
    """mode: ssr | embed | xhr. Returns (url, capture) mimicking a Lightpanda render capture."""
    url = f"{ORIGIN}/items/{it['id']}"
    rendered = page(it["title"], it["body"], it["specs"], it["related"])
    if mode == "ssr":
        raw = rendered; reqs = []
    elif mode == "embed":
        raw = page(it["title"], it["body"], it["specs"], it["related"], embed={"item": it}); rendered = raw; reqs = []
    else:
        raw = page(it["title"], it["body"], it["specs"], it["related"], csr=True)
        reqs = [{"url": f"{ORIGIN}/api/v2/products/{it['id']}", "method": "GET", "headers": {}, "post_data": None,
                 "status": 200, "mime": "application/json", "body": json.dumps({"product": it}), "type": "Fetch"}]
    doc = {"url": url, "method": "GET", "headers": {}, "post_data": None, "status": 200, "mime": "text/html", "body": raw, "type": "Document"}
    return url, {"url": url, "final_html": rendered, "status": 200, "requests": [doc] + reqs}


def samples(mode):
    from rsicrawl.isolate import build_profile, reference_record
    from rsicrawl.util import url_vars
    caps = [capture(it, mode) for it in ITEMS]
    prof = build_profile([(u, c["final_html"]) for u, c in caps])
    return [{"url": u, "vars": url_vars(u), "capture": c, "ref": reference_record(u, c["final_html"], prof)} for u, c in caps], prof
