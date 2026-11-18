import asyncio, json
from rsicrawl.recipe import run_recipe, StepCache, RecipeError

RECIPE = {"steps": [
    {"name": "cfg", "url": "{{origin}}/api/config", "parse": "json", "extract": {"base": {"path": "apiBase"}}},
    {"name": "data", "url": "{{origin}}{{base}}/items/{{seg1}}", "parse": "json"}],
    "output": {"title": {"step": "data", "path": "item.name", "first": True},
               "main_text": [{"step": "data", "path": "item.name"}, {"step": "data", "path": "item.text", "split": "\\n+"},
                             {"step": "data", "path": "item.specs", "kv": True}],
               "links": [{"step": "data", "path": "item.related[*]", "template": "{{origin}}/items/{{id}}"}]}}


def transport(calls):
    async def tx(method, url, headers, body):
        calls.append(url)
        if url.endswith("/api/config"): return 200, json.dumps({"apiBase": "/v9"})
        if "/v9/items/" in url:
            i = url.rsplit("/", 1)[1]
            return 200, json.dumps({"item": {"name": f"Item {i}", "text": "a b\n\nc d", "specs": {"price": "1"}, "related": [{"id": 5}]}})
        return 404, ""
    return tx


def vars_(i):
    return {"origin": "http://s.t", "seg1": str(i), "page_url": f"http://s.t/items/{i}"}


def test_chain_split_kv_template():
    rec, info = asyncio.run(run_recipe(RECIPE, "http://s.t/items/7", transport([]), vars_(7)))
    assert rec["title"] == "Item 7" and rec["main_text"] == "Item 7\na b\nc d\nprice: 1" and rec["links"] == ["http://s.t/items/5"]


def test_step_cache_skips_stable_chain_step():
    cache, calls = StepCache(stable_n=3), []
    for i in range(6):
        asyncio.run(run_recipe(RECIPE, f"http://s.t/items/{i}", transport(calls), vars_(i), cache=cache))
    assert sum(1 for c in calls if c.endswith("/api/config")) == 3      # skipped once stable
    assert sum(1 for c in calls if "/v9/items/" in c) == 6               # data step always runs


def test_http_error_raises():
    r = dict(RECIPE, steps=[dict(RECIPE["steps"][0], url="{{origin}}/missing")])
    try:
        asyncio.run(run_recipe(r, "http://s.t/items/1", transport([]), vars_(1))); assert False
    except RecipeError:
        pass
