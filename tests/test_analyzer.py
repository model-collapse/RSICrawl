import asyncio
from conftest import samples
from rsicrawl import analyzer as an


def test_xhr_source_gives_direct_api_recipe():
    s, prof = samples("xhr")
    best, rep = asyncio.run(an.analyze(s[:3], prof))
    assert best is not None and best["tier"] == "T0"
    assert best["steps"][0]["url"] in ("{{origin}}/api/v2/products/{{seg1}}", "{{origin}}/api/v2/products/{{last}}")
    n, _ = asyncio.run(an.verify_offline(best, s[3:])); assert n == 1   # generalizes to a held-out page


def test_embedded_json_gives_html_recipe():
    s, prof = samples("embed")
    best, _ = asyncio.run(an.analyze(s[:3], prof))
    assert best is not None and best["tier"] == "T1"


def test_excluded_candidates_are_not_reproposed():
    s, prof = samples("xhr")
    best, _ = asyncio.run(an.analyze(s[:3], prof))
    again, rep = asyncio.run(an.analyze(s[:3], prof, exclude={best["id"]}))
    assert again is None or again["id"] != best["id"]
