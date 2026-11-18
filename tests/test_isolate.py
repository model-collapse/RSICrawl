from conftest import samples


def test_reference_record_is_main_content_only():
    s, prof = samples("ssr")
    ref = s[0]["ref"]
    assert ref["title"] == "Red Kettle"
    assert "Boils water fast." in ref["main_text"] and "price: 19.99" in ref["main_text"]
    assert "(c) Shop" not in ref["main_text"] and "About" not in ref["main_text"]   # boilerplate excluded
    assert sorted(ref["links"]) == ["http://shop.test/items/12", "http://shop.test/items/13"]  # per-page links only
    assert "Shop" in prof["const_title_segs"]
