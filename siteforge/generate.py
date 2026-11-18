"""Generate a SiteForge benchmark: sites.json, urls/, truth/, server_data/.

    python -m siteforge.generate --seed 7 --out runs/forge7 [--sites-per-combo 1] [--items 120]
"""
import argparse
import json
import math
import os
import random

from .common import (CONTROL_ARCHETYPES, ITEMS_PER_LIST, SPEC_KEYS, base_url,
                     item_main_text, item_path, list_path, list_title)

BASE_COMBOS = [
    ("next", "A1"), ("nuxt", "A1"), ("next", "A2"), ("nuxt", "A2"), ("wp", "A2"),
    ("next", "A3"), ("wp", "A3"), ("spa", "A3"), ("gql", "A4"), ("spa", "A5"),
    ("spa", "A6"), ("spa", "A7"), ("spa", "A10"), ("spa", "A11"), ("spa", "A12"),
]
REPEAT_COMBOS = [
    ("next", "A2"), ("nuxt", "A2"), ("wp", "A3"), ("next", "A3"), ("spa", "A3"),
    ("spa", "A5"), ("gql", "A4"),
]
FIRST_PORT = 18001

ONSETS = ["b", "br", "c", "cl", "d", "dr", "f", "fl", "g", "gr", "h", "j", "k", "l", "m",
          "n", "p", "pl", "qu", "r", "s", "st", "t", "tr", "v", "w", "z", "sh", "ch", "th"]
VOWELS = ["a", "e", "i", "o", "u", "ai", "ea", "io", "ou", "y"]
CODAS = ["", "", "", "n", "r", "s", "l", "m", "x", "t", "nd", "rk", "st"]
ADJ = ["Acme", "Blue", "Copper", "Delta", "Ember", "Fable", "Granite", "Harbor", "Iron",
       "Juniper", "Kestrel", "Lumen", "Maple", "North", "Opal", "Pine", "Quarry", "River",
       "Summit", "Timber", "Upland", "Vale", "Willow", "Zephyr"]
NOUN = ["Gear", "Supply", "Outfitters", "Works", "Market", "Depot", "Goods", "Trading",
        "House", "Store", "Emporium", "Collective", "Mercantile", "Bazaar", "Co"]


def pword(rng: random.Random) -> str:
    return "".join(rng.choice(ONSETS) + rng.choice(VOWELS) + rng.choice(CODAS)
                   for _ in range(rng.choice([1, 2, 2, 3])))


def sentence(rng: random.Random) -> str:
    words = [pword(rng) for _ in range(rng.randint(6, 14))]
    words[0] = words[0].capitalize()
    return " ".join(words) + "."


def make_items(rng: random.Random, n: int, family: str) -> list[dict]:
    categories = [pword(rng).capitalize() for _ in range(6)]
    ids = rng.sample(range(1000, 99999), n)
    post_ids = rng.sample(range(100, 99999), n)
    items, slugs = [], set()
    for i in range(n):
        while True:
            words = [pword(rng) for _ in range(rng.randint(2, 4))]
            slug = "-".join(words)
            if slug not in slugs:
                slugs.add(slug)
                break
        title = " ".join(w.capitalize() for w in words)
        paras = [" ".join(sentence(rng) for _ in range(rng.randint(2, 5)))
                 for _ in range(rng.randint(2, 4))]
        specs = {
            "price": f"{rng.uniform(3, 400):.2f}",
            "sku": f"SKU-{rng.randint(1000, 9999)}-{rng.choice('ABCDEFGHJK')}{rng.randint(10, 99)}",
            "category": rng.choice(categories),
            "rating": f"{rng.uniform(1, 5):.1f}",
        }
        items.append({
            "id": ids[i], "slug": slug, "title": title, "paras": paras, "specs": specs,
            "post_id": post_ids[i],
            "date": f"{rng.randint(2018, 2025)}/{rng.randint(1, 12):02d}",
            "internal_id": "rec_" + "".join(rng.choice("0123456789abcdef") for _ in range(12)),
        })
    for i, it in enumerate(items):
        others = [j for j in range(n) if j != i]
        it["related"] = [items[j]["id"] for j in rng.sample(others, 3)]
    return items


def assign_splits(sites: list[dict], rng: random.Random) -> None:
    cycle = ["train", "test", "val"]
    fams: dict[str, list[dict]] = {}
    for s in sites:
        if s["archetype"] in CONTROL_ARCHETYPES:
            s["split"] = "test"
        else:
            fams.setdefault(s["family"], []).append(s)
    for fam in sorted(fams):
        group = fams[fam][:]
        rng.shuffle(group)
        for k, s in enumerate(group):
            s["split"] = cycle[k % 3] if len(group) > 1 else "train"


def generate(seed: int, out: str, sites_per_combo: int = 1, n_items: int = 120) -> list[dict]:
    master = random.Random(f"siteforge:{seed}")
    combos = BASE_COMBOS * sites_per_combo + REPEAT_COMBOS * sites_per_combo
    names = [f"{a} {b}" for a in ADJ for b in NOUN]
    master.shuffle(names)
    n_pages = math.ceil(n_items / ITEMS_PER_LIST)
    sites, datas = [], []
    for idx, (family, arch) in enumerate(combos):
        site_id = f"s{idx + 1:02d}"
        rng = random.Random(f"siteforge:{seed}:{site_id}:{family}:{arch}")
        port = FIRST_PORT + idx
        spec = {"site_id": site_id, "port": port, "family": family, "archetype": arch,
                "split": "train", "latency_ms": rng.choice(range(10, 65, 5)),
                "name": names[idx % len(names)], "patterns": ["item", "list"],
                "n_items": n_items, "n_list_pages": n_pages}
        items = make_items(rng, n_items, family)
        hexs = "0123456789abcdef"
        alnum = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        data = {
            "site_id": site_id, "seed": seed,
            "csrf_token": "".join(rng.choice(hexs) for _ in range(40)),
            "a10_secret": "".join(rng.choice(alnum) for _ in range(24)),
            "build_ids": ["".join(rng.choice(alnum + "_-") for _ in range(21)) for _ in range(8)],
            "items": items,
        }
        sites.append(spec)
        datas.append(data)
    assign_splits(sites, random.Random(f"siteforge:{seed}:splits"))

    for sub in ("urls", "truth", "server_data"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    for spec, data in zip(sites, datas):
        write_site(spec, data, out)
    with open(os.path.join(out, "sites.json"), "w") as f:
        json.dump(sites, f, indent=1)
    return sites


def write_site(spec: dict, data: dict, out: str) -> None:
    fam, port, sid = spec["family"], spec["port"], spec["site_id"]
    base = base_url(port)
    items = data["items"]
    by_id = {it["id"]: it for it in items}
    blocked = spec["archetype"] == "A11"
    records = []
    for it in items:
        records.append({
            "url": base + item_path(fam, it), "pattern": "item", "title": it["title"],
            "main_text": item_main_text(it), "fields": dict(it["specs"]),
            "links": [base + item_path(fam, by_id[r]) for r in it["related"]],
            "blocked": blocked,
        })
    n_pages = spec["n_list_pages"]
    for p in range(1, n_pages + 1):
        chunk = items[(p - 1) * ITEMS_PER_LIST: p * ITEMS_PER_LIST]
        title = list_title(p, n_pages)
        links = [base + item_path(fam, it) for it in chunk]
        if p < n_pages:
            links.append(base + list_path(p + 1))
        records.append({
            "url": base + list_path(p), "pattern": "list", "title": title,
            "main_text": title,  # link texts are not main text; links are scored separately
            "fields": {}, "links": links, "blocked": blocked,
        })
    random.Random(f"siteforge:{data['seed']}:{sid}:order").shuffle(records)
    with open(os.path.join(out, "urls", f"{sid}.txt"), "w") as f:
        f.write("".join(r["url"] + "\n" for r in records))
    with open(os.path.join(out, "truth", f"{sid}.jsonl"), "w") as f:
        f.write("".join(json.dumps(r) + "\n" for r in records))
    with open(os.path.join(out, "server_data", f"{sid}.json"), "w") as f:
        json.dump({"spec": spec, **data}, f)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sites-per-combo", type=int, default=1)
    ap.add_argument("--items", type=int, default=120)
    a = ap.parse_args()
    sites = generate(a.seed, a.out, a.sites_per_combo, a.items)
    for s in sites:
        print(f"{s['site_id']} :{s['port']} {s['family']:5} {s['archetype']:4} {s['split']:5} {s['name']}")
    print(f"wrote {len(sites)} sites to {a.out}")


if __name__ == "__main__":
    main()
