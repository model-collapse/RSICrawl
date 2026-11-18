"""Shared helpers: URL shapes, spec keys, and text assembly for truth records."""

SPEC_KEYS = ["price", "sku", "category", "rating"]
CONTROL_ARCHETYPES = {"A10", "A11", "A12"}
ITEMS_PER_LIST = 20


def item_path(family: str, it: dict) -> str:
    if family == "next":
        return f"/items/{it['id']}/{it['slug']}"
    if family == "nuxt":
        return f"/p/{it['slug']}-{it['id']}"
    if family == "wp":
        return f"/{it['date']}/{it['slug']}/"
    if family == "spa":
        return f"/item/{it['id']}"
    if family == "gql":
        return f"/products/{it['slug']}"
    raise ValueError(family)


def list_path(page: int) -> str:
    return f"/list?page={page}"


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def item_main_text(it: dict) -> str:
    lines = [it["title"], *it["paras"], *[f"{k}: {it['specs'][k]}" for k in SPEC_KEYS]]
    return "\n".join(lines)


def list_title(page: int, n_pages: int) -> str:
    return f"Catalog page {page} of {n_pages}"
