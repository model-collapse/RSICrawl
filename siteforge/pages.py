"""HTML page builders and client-side JS for SiteForge sites."""
import json
from html import escape

from .common import SPEC_KEYS, ITEMS_PER_LIST, item_path, list_path, list_title
from . import shapes
from .sha256js import SHA256_JS

STYLES = [
    {"wrap": "", "article": "item", "body": "body", "specs": "specs", "related": "related",
     "items": "items", "pager": "pager", "nav": "site-nav"},
    {"wrap": "layout-v2", "article": "product-card", "body": "pc-desc", "specs": "pc-attrs",
     "related": "pc-links", "items": "grid-list", "pager": "pc-pager", "nav": "topbar"},
]


def js_json(obj) -> str:
    """JSON safe for embedding inside <script>."""
    return json.dumps(obj).replace("</", "<\\/")


SUBRES = int(__import__("os").environ.get("SITEFORGE_SUBRES", "0"))  # realistic sub-resources per page (0 = light)


def subresources():
    """K external script bundles + analytics beacon + 2 widget XHRs. Never touches main content (truth unchanged)."""
    if SUBRES <= 0: return "", ""
    head = "".join(f'<script src="/static/bundle-{i}.js" defer></script>' for i in range(SUBRES))
    tail = ("<script>(function(){var u=encodeURIComponent(location.pathname);"
            "fetch('/t/collect',{method:'POST',body:JSON.stringify({p:location.pathname,t:Date.now()})}).catch(function(){});"
            "fetch('/_svc/recs?u='+u).catch(function(){});fetch('/_svc/reviews?u='+u).catch(function(){});})();</script>")
    return head, tail


def layout(spec, st, title, main_inner, head_extra="", body_attrs="", tail=""):
    c = STYLES[st["style"]]
    _h, _t = subresources(); head_extra = head_extra + _h; tail = tail + _t
    name = escape(spec["name"])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">{head_extra}</head>
<body{body_attrs}>
<header class="{c['nav']}"><a class="brand" href="/">{name}</a>
<nav><a href="/">Home</a> <a href="/list?page=1">Catalog</a> <a href="/about">About</a></nav></header>
<main id="content">{main_inner}</main>
<footer><p>&copy; 2025 {name}. All rights reserved.</p><a href="/about">About us</a> <a href="/robots.txt">robots</a></footer>
{tail}</body></html>
"""


def item_main(st, it, rel, family):
    c = STYLES[st["style"]]
    paras = "".join(f"<p>{escape(p)}</p>" for p in it["paras"])
    specs = "".join(f"<li>{k}: {escape(it['specs'][k])}</li>" for k in SPEC_KEYS)
    links = "".join(f'<a href="{item_path(family, r)}">{escape(r["title"])}</a>' for r in rel)
    inner = (f'<article class="{c["article"]}"><h1>{escape(it["title"])}</h1>'
             f'<div class="{c["body"]}">{paras}</div><ul class="{c["specs"]}">{specs}</ul>'
             f'<div class="{c["related"]}">{links}</div></article>')
    return f'<div class="{c["wrap"]}">{inner}</div>' if c["wrap"] else inner


def list_page(spec, st, items, page):
    c = STYLES[st["style"]]
    n = spec["n_list_pages"]
    chunk = items[(page - 1) * ITEMS_PER_LIST: page * ITEMS_PER_LIST]
    lis = "".join(f'<li><a href="{item_path(spec["family"], it)}">{escape(it["title"])}</a></li>'
                  for it in chunk)
    nxt = (f'<nav class="{c["pager"]}"><a class="next" rel="next" href="{list_path(page + 1)}">Next page</a></nav>'
           if page < n else "")
    title = list_title(page, n)
    main = f'<h1>{title}</h1><ul class="{c["items"]}">{lis}</ul>{nxt}'
    return layout(spec, st, f"{title} | {spec['name']}", main)


def home_page(spec, st):
    main = (f"<h1>Welcome to {escape(spec['name'])}</h1><p>Browse our catalog of fine goods.</p>"
            '<p><a href="/list?page=1">Start browsing the catalog</a></p>')
    return layout(spec, st, spec["name"], main)


def about_page(spec, st):
    main = (f"<h1>About {escape(spec['name'])}</h1>"
            "<p>We are a small independent shop. This site is synthetic.</p>")
    return layout(spec, st, f"About | {spec['name']}", main)


def challenge_page():
    return """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div id="challenge"><h1>Checking your browser&hellip;</h1>
<p>Please complete the captcha to continue. This process is automatic.</p>
<div class="captcha" data-sitekey="0x4AAAAAAA">captcha</div></div></body></html>
"""


def csrf_meta(spec, st, data):
    return f'\n<meta name="csrf-token" content="{data["csrf_token"]}">' if st["csrf"] else ""


# ---- SSR item pages -------------------------------------------------------------------
def item_page_ssr(spec, st, data, it, rel):
    fam, port = spec["family"], spec["port"]
    head, tail = csrf_meta(spec, st, data), ""
    if st["mode"] == "embed":
        if fam == "next":
            nd = shapes.next_data(it, rel, st, data["build_ids"][st["build_idx"]])
            tail = f'<script id="__NEXT_DATA__" type="application/json">{js_json(nd)}</script>\n'
        elif fam == "nuxt":
            tail = f"<script>window.__NUXT__={js_json(shapes.nuxt_state(it, rel, st))}</script>\n"
        elif fam == "wp":
            head += (f'\n<script type="application/ld+json">{js_json(shapes.wp_ldjson(it, rel, st, port))}</script>')
    return layout(spec, st, f"{it['title']} | {spec['name']}", item_main(st, it, rel, fam),
                  head_extra=head, tail=tail)


# ---- CSR item pages -------------------------------------------------------------------
JS_CORE = r"""
var KEYS=["price","sku","category","rating"];
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e;}
function hdrs(extra){var h={"Accept":"application/json"};var m=document.querySelector('meta[name="csrf-token"]');
 if(m)h["X-CSRF-Token"]=m.getAttribute("content");if(extra){for(var k in extra)h[k]=extra[k];}return h;}
function getJSON(u,o){o=o||{};o.credentials="same-origin";o.headers=hdrs(o.headers);
 return fetch(u,o).then(function(r){if(!r.ok)throw new Error("HTTP "+r.status+" for "+u);return r.json();});}
function render(d){
 document.title=d.t+" | "+C.site;
 var m=document.getElementById("content");m.textContent="";
 var a=el("article",C.cls.article);a.appendChild(el("h1",null,d.t));
 var b=el("div",C.cls.body);d.p.forEach(function(p){b.appendChild(el("p",null,p));});a.appendChild(b);
 var u=el("ul",C.cls.specs);d.s.forEach(function(s){u.appendChild(el("li",null,s[0]+": "+s[1]));});a.appendChild(u);
 var r=el("div",C.cls.related);d.r.forEach(function(x){var l=el("a",null,x[1]);l.setAttribute("href",x[0]);r.appendChild(l);});a.appendChild(r);
 if(C.cls.wrap){var w=el("div",C.cls.wrap);w.appendChild(a);m.appendChild(w);}else{m.appendChild(a);}
}
function fail(e){var m=document.getElementById("content");m.textContent="";m.appendChild(el("p","error","Failed to load: "+e));}
function lastSeg(){var p=location.pathname.replace(/\/+$/,"").split("/");return p[p.length-1];}
"""

LOADERS = {
    "next": r"""
var nd=JSON.parse(document.getElementById("__NEXT_DATA__").textContent);
getJSON("/_next/data/"+nd.buildId+"/items/"+nd.query.id+".json").then(function(j){var it=j.pageProps.item;
 render({t:it[C.tk],p:it.body,s:KEYS.map(function(k){return [k,it.specs[k]];}),
  r:it.related.map(function(x){return ["/items/"+x.id+"/"+x.slug,x[C.tk]];})});}).catch(fail);
""",
    "nuxt": r"""
var pid=lastSeg().split("-").pop();
getJSON(C.api+"/"+pid).then(function(it){
 render({t:it[C.tk],p:it.content,s:it.specs.map(function(x){return [x.label,x.value];}),
  r:it.related.map(function(x){return ["/p/"+x.slug+"-"+x.id,x[C.tk]];})});}).catch(fail);
""",
    "wp": r"""
var postId=document.body.getAttribute("data-post-id");
getJSON(C.api+"/posts/"+postId).then(function(j){
 var ps=[],re=/<p>([\s\S]*?)<\/p>/g,m;while((m=re.exec(j.content.rendered))!==null){ps.push(m[1].replace(/<[^>]+>/g,""));}
 render({t:j.title.rendered,p:ps,s:KEYS.map(function(k){return [k,j[C.tk][k]];}),
  r:j.related.map(function(x){return [x.link,x.title.rendered];})});}).catch(fail);
""",
    "spa": r"""
function S(j){var it=j.item;render({t:it[C.tk],p:it.paragraphs,s:KEYS.map(function(k){return [k,it[k]];}),
 r:it.related.map(function(x){return ["/item/"+x.id,x[C.tk]];})});}
var iid=lastSeg();
if(C.mode==="session"){
 fetch("/api/session",{credentials:"same-origin"}).then(function(r){if(!r.ok)throw new Error("session "+r.status);
  return getJSON(C.api+"/items/"+iid);}).then(S).catch(fail);
}else if(C.mode==="signed"){
 var ts=Math.floor(Date.now()/1000);var sig=sha256(SECRET+iid+ts);
 getJSON(C.api+"/items/"+iid+"?ts="+ts+"&sig="+sig).then(S).catch(fail);
}else{getJSON(C.api+"/items/"+iid).then(S).catch(fail);}
""",
    "a7": r"""
var base;
getJSON("/api/config").then(function(c){base=c.apiBase;
 return getJSON(base+"/lookup?slug="+encodeURIComponent(window.__PAGE__.slug));})
.then(function(l){return getJSON(base+"/records/"+encodeURIComponent(l.internalId));})
.then(function(j){var r=j.record;render({t:r[C.tk],p:r.text.split("\n\n"),s:KEYS.map(function(k){return [k,r.attrs[k]];}),
 r:r.links.map(function(x){return ["/item/"+x.id,x[C.tk]];})});}).catch(fail);
""",
    "gql": r"""
getJSON(C.api,{method:"POST",headers:{"Content-Type":"application/json"},
 body:JSON.stringify({query:C.query,variables:{slug:decodeURIComponent(lastSeg())}})})
.then(function(j){if(j.errors)throw new Error(j.errors[0].message);var p=j.data.product;
 render({t:p.name,p:p.description.split("\n\n"),s:p[C.tk].map(function(a){return [a.key,a.value];}),
  r:p.related.map(function(x){return ["/products/"+x.slug,x.name];})});}).catch(fail);
""",
}


def item_page_csr(spec, st, data, it, rel):
    fam, arch = spec["family"], spec["archetype"]
    cfg = {"site": spec["name"], "cls": STYLES[st["style"]], "mode": "plain"}
    head, body_attrs, pre = csrf_meta(spec, st, data), "", ""
    loader = fam
    if fam == "next":
        nd = shapes.next_data(it, rel, st, data["build_ids"][st["build_idx"]], with_props=False)
        pre = f'<script id="__NEXT_DATA__" type="application/json">{js_json(nd)}</script>\n'
        cfg["tk"] = "headline" if st["renamed"] else "title"
    elif fam == "nuxt":
        cfg.update(api=shapes.nuxt_api(st), tk="name" if st["renamed"] else "title")
    elif fam == "wp":
        cfg.update(api=shapes.wp_ns(st), tk="acf" if st["renamed"] else "meta")
        body_attrs = f' class="post-template-default single single-post postid-{it["post_id"]}" data-post-id="{it["post_id"]}"'
    elif fam == "gql":
        cfg.update(api=shapes.gql_path(st), query=shapes.gql_query(st),
                   tk="specs" if st["renamed"] else "attributes")
    elif arch == "A7":
        loader = "a7"
        cfg["tk"] = "heading" if st["renamed"] else "label"
        pre = f"<script>window.__PAGE__={js_json({'slug': it['slug'], 'type': 'item'})}</script>\n"
    else:  # spa A3/A5/A6/A10/A12
        cfg.update(api=shapes.spa_api(st), tk="name" if st["renamed"] else "title")
        if arch == "A6":
            cfg["mode"] = "session"
        elif arch == "A10":
            cfg["mode"] = "signed"
    js = f"var C={js_json(cfg)};\n" + JS_CORE
    if arch == "A10":
        js = f'var SECRET="{data["a10_secret"]}";\n' + SHA256_JS + js
    js += LOADERS[loader]
    tail = pre + f"<script>\n(function(){{\n{js}\n}})();\n</script>\n"
    return layout(spec, st, spec["name"], "", head_extra=head, body_attrs=body_attrs, tail=tail)
