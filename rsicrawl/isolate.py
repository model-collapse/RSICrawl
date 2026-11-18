"""Content isolation: main content = what varies across pages of the same pattern.

A page is decomposed into text *blocks* keyed by a structural signature (tag.class chain, no indices).
Across >= 3 samples, signatures whose text is identical everywhere are template boilerplate; signatures
whose text varies are main content. Anchors are handled separately: per-page links vs site-constant links.
"""
import re
from collections import defaultdict
from urllib.parse import urljoin
from bs4 import BeautifulSoup, NavigableString, Tag

SKIP = {"script", "style", "noscript", "template", "head", "svg"}
BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "dd", "dt", "pre", "blockquote", "figcaption",
         "div", "section", "article", "main", "header", "footer", "nav", "aside", "ul", "ol", "table", "tr"}


def _sig(el):
    parts = []
    while el is not None and isinstance(el, Tag) and el.name not in ("[document]", "html"):
        cls = ".".join(sorted(c for c in el.get("class", []) if not re.search(r"\d", c)))
        idv = el.get("id")
        parts.append(el.name + (f"#{idv}" if idv and not re.search(r"\d", idv) else "") + (f".{cls}" if cls else ""))
        el = el.parent
    return ">".join(reversed(parts))


def _text_excluding_anchors(el):
    out = []
    for c in el.children:
        if isinstance(c, NavigableString):
            if c.parent.name not in SKIP: out.append(str(c))
        elif isinstance(c, Tag) and c.name not in SKIP and c.name != "a" and c.name not in BLOCK:
            out.append(_text_excluding_anchors(c))
    return " ".join(" ".join(out).split())


def blocks(html, base_url):
    """Returns (title, text_blocks[(sig, text)], anchors[(sig, href_abs, text)])."""
    soup = BeautifulSoup(html or "", "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    body = soup.body or soup
    tb, an = [], []
    for el in body.find_all(True):
        if el.name in SKIP or any(p.name in SKIP for p in el.parents if isinstance(p, Tag)): continue
        if el.name == "a" and el.get("href"):
            an.append((_sig(el), urljoin(base_url, el["href"]), el.get_text(" ", strip=True)))
            continue
        if el.name in BLOCK:
            t = _text_excluding_anchors(el)
            if t: tb.append((_sig(el), t))
    return title, tb, an


def _strip_common_affix(titles):
    if len(titles) < 2: return titles
    pre = min(titles, key=len)
    while pre and not all(t.startswith(pre) for t in titles): pre = pre[:-1]
    suf = min(titles, key=len)
    while suf and not all(t.endswith(suf) for t in titles): suf = suf[1:]
    out = []
    for t in titles:
        x = t[len(pre):] if pre and len(pre) < len(t) else t
        x = x[:-len(suf)] if suf and len(suf) < len(x) else x
        out.append(x.strip(" |-–—:"))
    return out


def build_profile(samples):
    """samples: list of (url, html). Returns an isolation profile for the pattern."""
    per_sig_text = defaultdict(list); per_sig_links = defaultdict(list); titles = []
    n = len(samples)
    for url, html in samples:
        title, tb, an = blocks(html, url)
        titles.append(title)
        agg = defaultdict(list)
        for s, t in tb: agg[s].append(t)
        for s in set(list(agg.keys())): per_sig_text[s].append("\n".join(agg[s]))
        lagg = defaultdict(list)
        for s, h, _ in an: lagg[s].append(h)
        for s in lagg: per_sig_links[s].append(tuple(sorted(lagg[s])))
    var_text = sorted(s for s, vals in per_sig_text.items() if len(set(vals)) > 1 or len(vals) < n)
    const_text = sorted(s for s, vals in per_sig_text.items() if len(set(vals)) == 1 and len(vals) == n)
    var_links = sorted(s for s, vals in per_sig_links.items() if len(set(vals)) > 1 or len(vals) < n)
    const_links = sorted(s for s, vals in per_sig_links.items() if len(set(vals)) == 1 and len(vals) == n)
    segs = [set(_SEP.split(t)) for t in titles if t]
    const_title_segs = sorted(set.intersection(*segs)) if len(segs) >= 2 else []
    # Drop variable signatures that are ancestors of other variable signatures (avoid double text)
    var_text = [s for s in var_text if not any(o != s and o.startswith(s + ">") for o in var_text)]
    return {"var_text": var_text, "const_text": const_text, "var_links": var_links, "const_links": const_links,
            "const_title_segs": const_title_segs, "n": n}


_SEP = re.compile(r"\s+[|\u2013\u2014-]\s+")
_BOILER = re.compile(r"(^|>)(header|footer|nav)([#.>]|$)")


def reference_record(url, html, profile=None):
    """Main-content record for one rendered page. Without a profile, a header/footer/nav heuristic is used."""
    title, tb, an = blocks(html, url)
    if profile and profile.get("n", 0) >= 3:
        vt = set(profile["var_text"]); vl = set(profile["var_links"])
        texts = [t for s, t in tb if s in vt]
        links = [h for s, h, _ in an if s in vl]
        cs = set(profile.get("const_title_segs") or [])
        parts = [x for x in _SEP.split(title) if x not in cs] if title else []
        title = " - ".join(parts) if parts else (texts[0] if texts else title)
    else:
        texts = [t for s, t in tb if not _BOILER.search(s)]
        links = [h for s, h, _ in an if not _BOILER.search(s)]
        title = _SEP.split(title)[0] if title else title
    seen, main = set(), []
    for t in texts:
        if t not in seen: seen.add(t); main.append(t)
    dl, seen_l = [], set()
    for h in links:
        if h not in seen_l: seen_l.add(h); dl.append(h)
    return {"title": title, "main_text": "\n".join(main), "fields": {}, "links": dl}


def site_constant_links(url, html, profile):
    _, _, an = blocks(html, url)
    cl = set(profile.get("const_links", []))
    return sorted({h for s, h, _ in an if s in cl})
