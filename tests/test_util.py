from rsicrawl.util import url_pattern, url_vars, token_f1, link_recall, equivalent


def test_url_pattern_abstracts_ids_and_slugs():
    assert url_pattern("http://a.b/items/123/red-kettle") == "a.b/items/{num}/{str}"
    assert url_pattern("http://a.b/list?page=3") == "a.b/list?page={num}"
    assert url_pattern("http://a.b/about") == "a.b/about"


def test_url_vars_split_slug_id():
    v = url_vars("http://a.b/p/red-kettle-42")
    assert v["origin"] == "http://a.b" and v["seg1_num"] == "42" and v["seg1_head"] == "red-kettle"


def test_token_f1_and_link_recall():
    assert token_f1("a b c", "a b c") == 1.0 and token_f1("", "") == 1.0 and token_f1("x", "y") == 0.0
    assert link_recall(["http://a/x"], ["http://a/x", "http://a/y"]) == 0.5


def test_equivalent_checks_title_and_fields():
    ref = {"title": "T", "main_text": "one two three", "links": ["http://a/1"], "fields": {"price": "1"}}
    ok, _ = equivalent({"title": "T", "main_text": "one two three", "links": ["http://a/1"]}, ref); assert ok
    ok, d = equivalent({"title": "X", "main_text": "one two three", "links": ["http://a/1"]}, ref); assert not ok and not d["title_ok"]
    ok, _ = equivalent({"title": "T", "main_text": "one two three", "links": ["http://a/1"], "fields": {"price": "2"}}, ref); assert not ok
