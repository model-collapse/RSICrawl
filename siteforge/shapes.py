"""Per-family JSON shapes (embedded state and API payloads) and drift-dependent paths."""
from .common import SPEC_KEYS, base_url, item_path


def fresh_state(spec: dict) -> dict:
    arch = spec["archetype"]
    mode = {"A1": "ssr", "A2": "embed", "A11": "ssr"}.get(arch, "csr")
    return {"mode": mode, "style": 0, "api_bump": 0, "renamed": False,
            "csrf": arch == "A5", "build_idx": 0}


# ---- drift-dependent API paths ------------------------------------------------------
def spa_api(st):   return f"/api/v{1 + st['api_bump']}"
def a7_base(st):   return f"/svc/v{3 + st['api_bump']}"
def wp_ns(st):     return f"/wp-json/wp/v{2 + st['api_bump']}"
def nuxt_api(st):  return f"/api/v{1 + st['api_bump']}/products"
def gql_path(st):  return "/graphql" if st["api_bump"] == 0 else f"/v{1 + st['api_bump']}/graphql"


# ---- shapes -------------------------------------------------------------------------
def next_item(it, rel, st):
    tk = "headline" if st["renamed"] else "title"
    return {"id": it["id"], "slug": it["slug"], tk: it["title"], "body": it["paras"],
            "specs": {k: it["specs"][k] for k in SPEC_KEYS},
            "related": [{"id": r["id"], "slug": r["slug"], tk: r["title"]} for r in rel]}


def next_data(it, rel, st, build_id, with_props=True):
    return {"props": {"pageProps": {"item": next_item(it, rel, st)} if with_props else {}},
            "page": "/items/[id]/[slug]", "query": {"id": str(it["id"]), "slug": it["slug"]},
            "buildId": build_id, "isFallback": not with_props, "gssp": True}


def nuxt_item(it, rel, st):
    tk = "name" if st["renamed"] else "title"
    return {"id": it["id"], "slug": it["slug"], tk: it["title"], "content": it["paras"],
            "specs": [{"label": k, "value": it["specs"][k]} for k in SPEC_KEYS],
            "related": [{"id": r["id"], "slug": r["slug"], tk: r["title"]} for r in rel]}


def nuxt_state(it, rel, st):
    return {"data": {f"product-{it['id']}": nuxt_item(it, rel, st)}, "state": {},
            "serverRendered": True, "config": {"app": {"baseURL": "/"}}}


def wp_post(it, rel, st, port):
    mk = "acf" if st["renamed"] else "meta"
    yyyy, mm = it["date"].split("/")
    return {"id": it["post_id"], "date": f"{yyyy}-{mm}-01T09:00:00", "slug": it["slug"],
            "status": "publish", "type": "post",
            "link": base_url(port) + item_path("wp", it),
            "title": {"rendered": it["title"]},
            "content": {"rendered": "".join(f"<p>{p}</p>\n" for p in it["paras"]), "protected": False},
            mk: {k: it["specs"][k] for k in SPEC_KEYS},
            "related": [{"id": r["post_id"], "title": {"rendered": r["title"]},
                         "link": base_url(port) + item_path("wp", r)} for r in rel]}


def wp_ldjson(it, rel, st, port):
    dk = "articleBody" if st["renamed"] else "description"
    return {"@context": "https://schema.org", "@type": "Product", "name": it["title"],
            dk: "\n\n".join(it["paras"]), "sku": it["specs"]["sku"],
            "category": it["specs"]["category"],
            "offers": {"@type": "Offer", "price": it["specs"]["price"], "priceCurrency": "USD"},
            "aggregateRating": {"@type": "AggregateRating", "ratingValue": it["specs"]["rating"]},
            "url": base_url(port) + item_path("wp", it),
            "isRelatedTo": [{"@type": "Product", "name": r["title"],
                             "url": base_url(port) + item_path("wp", r)} for r in rel]}


def spa_item(it, rel, st):
    tk = "name" if st["renamed"] else "title"
    d = {"id": it["id"], "slug": it["slug"], tk: it["title"], "paragraphs": it["paras"]}
    d.update({k: it["specs"][k] for k in SPEC_KEYS})
    d["related"] = [{"id": r["id"], tk: r["title"]} for r in rel]
    return {"item": d}


def a7_record(it, rel, st):
    lk = "heading" if st["renamed"] else "label"
    return {"record": {"internalId": it["internal_id"], lk: it["title"],
                       "text": "\n\n".join(it["paras"]),
                       "attrs": {k: it["specs"][k] for k in SPEC_KEYS},
                       "links": [{"id": r["id"], lk: r["title"]} for r in rel]}}


def gql_product(it, rel, st):
    ak = "specs" if st["renamed"] else "attributes"
    return {"id": str(it["id"]), "slug": it["slug"], "name": it["title"],
            "description": "\n\n".join(it["paras"]),
            ak: [{"key": k, "value": it["specs"][k]} for k in SPEC_KEYS],
            "related": [{"slug": r["slug"], "name": r["title"]} for r in rel]}


def gql_query(st):
    ak = "specs" if st["renamed"] else "attributes"
    return ("query Item($slug:String!){product(slug:$slug){id slug name description "
            f"{ak}{{key value}} related{{slug name}}}}}}")
