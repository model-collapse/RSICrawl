
import json, re, html as _html
from urllib.parse import urlsplit, urljoin, parse_qsl, quote

BLOCK = re.compile(r'</?(?:p|div|h[1-6]|li|ul|ol|br|tr|table|section|article|blockquote|pre)\b[^>]*>', re.I)

def html_lines(s):
    s = BLOCK.sub('\n', s)
    s = re.sub(r'<[^>]+>', '', s)
    s = _html.unescape(s)
    return [l.strip() for l in s.split('\n') if l.strip()]

def norm(s):
    return re.sub(r'\s+', ' ', str(s)).strip()

def walk(o, p, out):
    if isinstance(o, dict):
        out.append((p, o))
        for k, v in o.items():
            if not re.match(r'^[A-Za-z_][\w-]*$', str(k)):
                continue
            walk(v, (p + '.' if p else '') + k, out)
    elif isinstance(o, list):
        out.append((p, o))
        for v in o:
            walk(v, p + '[*]', out)
    else:
        out.append((p, o))

def body_json(r):
    try:
        b = r.get('body')
        if isinstance(b, (dict, list)):
            return b
        return json.loads(b)
    except Exception:
        return None

def find_api(sample):
    t = norm(sample['ref'].get('title', ''))
    reqs = sample.get('requests') or []
    for i, r in enumerate(reqs):
        if i == 0:
            continue
        j = body_json(r)
        if j is None:
            continue
        leaves = []
        walk(j, '', leaves)
        if any(isinstance(v, str) and norm(v) == t for p, v in leaves):
            return i, r, j, leaves
    return None

def resolve_value(val, samples_vals, samples):
    for name in samples[0].get('vars', {}):
        if all(str(s['vars'].get(name)) == v for s, v in zip(samples, samples_vals)):
            return ('lit', '{{%s}}' % name, None)
    if all(v == samples_vals[0] for v in samples_vals):
        return ('lit', samples_vals[0], None)
    page0 = samples[0]['requests'][0].get('body') or ''
    cands = []
    for m in re.finditer(r'([\w:-]+)=["\']' + re.escape(samples_vals[0]) + r'["\']', page0):
        attr = m.group(1)
        cands.append(r'\s' + re.escape(attr) + r'=["\']([^"\']*)["\']')
    idx = page0.find(samples_vals[0])
    while idx >= 0 and len(cands) < 30:
        for n in (8, 15, 25):
            pre = page0[max(0, idx - n):idx]
            if pre:
                cands.append(re.escape(pre) + r'([^"\'<>\s&,;)]+)')
        idx = page0.find(samples_vals[0], idx + 1)
    for rx in cands:
        ok = True
        for s, v in zip(samples, samples_vals):
            pb = s['requests'][0].get('body') or ''
            m = re.search(rx, pb)
            if not m or m.group(1) != v:
                ok = False
                break
        if ok:
            return ('var', None, rx)
    return None

def generate(evidence):
    samples = [s for s in evidence if s.get('requests')]
    if not samples:
        return []
    found = [find_api(s) for s in samples]
    if any(f is None for f in found):
        return []
    urls = [f[1]['url'] for f in found]
    parts = [urlsplit(u) for u in urls]
    segs = [p.path.split('/') for p in parts]
    if len(set(len(s) for s in segs)) != 1:
        return []
    extract = {}
    tpl_segs = []
    nvar = 0
    for k in range(len(segs[0])):
        vals = [s[k] for s in segs]
        if vals[0] == '' and all(v == '' for v in vals):
            tpl_segs.append('')
            continue
        r = resolve_value(vals[0], vals, samples)
        if r is None:
            return []
        if r[0] == 'lit':
            tpl_segs.append(r[1])
        else:
            nm = 'v%d' % nvar; nvar += 1
            extract[nm] = {'regex': r[2], 'first': True}
            tpl_segs.append('{{%s}}' % nm)
    q = ''
    qs = [parse_qsl(p.query, keep_blank_values=True) for p in parts]
    if qs[0]:
        if len(set(tuple(k for k, _ in x) for x in qs)) != 1:
            return []
        qparts = []
        for i, (k, _) in enumerate(qs[0]):
            vals = [x[i][1] for x in qs]
            r = resolve_value(vals[0], vals, samples)
            if r is None:
                return []
            if r[0] == 'lit':
                qparts.append(k + '=' + (r[1] if r[1].startswith('{{') else quote(r[1], safe='')))
            else:
                nm = 'v%d' % nvar; nvar += 1
                extract[nm] = {'regex': r[2], 'first': True}
                qparts.append(k + '=' + '{{%s}}' % nm)
        q = '?' + '&'.join(qparts)
    origin0 = samples[0]['vars'].get('origin', '')
    api_origin = parts[0].scheme + '://' + parts[0].netloc
    base = '{{origin}}' if api_origin == origin0 else api_origin
    api_url = base + '/'.join(tpl_segs) + q
    headers = {'Accept': 'application/json'}
    for hk, hv in (found[0][1].get('headers') or {}).items():
        lk = hk.lower()
        if lk in ('accept', 'user-agent', 'referer', 'cookie', 'accept-language', 'accept-encoding', 'origin', 'content-type', 'content-length', 'host', 'connection') or lk.startswith('sec-'):
            continue
        vals = [(f[1].get('headers') or {}).get(hk, '') for f in found]
        r = resolve_value(vals[0], vals, samples)
        if r is None:
            continue
        if r[0] == 'lit':
            headers[hk] = r[1]
        else:
            nm = 'h%d' % nvar; nvar += 1
            extract[nm] = {'regex': r[2], 'first': True}
            headers[hk] = '{{%s}}' % nm
    s0 = samples[0]
    ref = s0['ref']
    title = norm(ref.get('title', ''))
    reflines = [norm(l) for l in (ref.get('main_text') or '').split('\n') if norm(l)]
    refset = set(reflines)
    leaves = found[0][3]
    tpaths = [p for p, v in leaves if isinstance(v, str) and norm(v) == title and '[*]' not in p]
    if not tpaths:
        return []
    tpath = min(tpaths, key=len)
    mt = []
    def pos(line):
        try:
            return reflines.index(line)
        except ValueError:
            return 10**6
    mt.append((pos(title), {'step': 'api', 'path': tpath}))
    seenlist = set()
    for p, v in leaves:
        if p == tpath:
            continue
        if isinstance(v, str) and len(v) > 20:
            if '<' in v and '>' in v:
                ls = [norm(l) for l in html_lines(v)]
                if ls and sum(l in refset for l in ls) >= 0.8 * len(ls):
                    mt.append((pos(ls[0]), {'step': 'api', 'path': p, 'html_text': True}))
                    continue
            ls = [norm(l) for l in re.split(r'\n+', v) if norm(l)]
            if ls and all(l in refset for l in ls) and '[*]' not in p:
                ex = {'step': 'api', 'path': p}
                if len(ls) > 1:
                    ex['split'] = '\n+'
                mt.append((pos(ls[0]), ex))
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            ls = [norm(x) for x in v]
            if all(l in refset for l in ls):
                mt.append((pos(ls[0]), {'step': 'api', 'path': p + '[*]'}))
        elif isinstance(v, dict) and v and all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in v.values()):
            ls = ['%s: %s' % (k, x) for k, x in v.items()]
            if all(norm(l) in refset for l in ls):
                mt.append((pos(norm(ls[0])), {'step': 'api', 'path': p, 'kv': True}))
    mt.sort(key=lambda x: x[0])
    main_text = [e for _, e in mt]
    reflinks = set(ref.get('links') or [])
    page_url = s0['vars'].get('page_url', s0['url'])
    links = []
    if reflinks:
        for p, v in leaves:
            if '[*]' not in p or not isinstance(v, str):
                continue
            vals = [x for pp, x in leaves if pp == p and isinstance(x, str)]
            if not vals or p in seenlist:
                continue
            seenlist.add(p)
            if set(vals) <= reflinks:
                links.append({'step': 'api', 'path': p})
            elif set(urljoin(page_url, x) for x in vals) <= reflinks:
                links.append({'step': 'api', 'path': p, 'abs': True})
    steps = [{'name': 'page', 'method': 'GET', 'url': '{{page_url}}', 'parse': 'html', 'extract': extract},
             {'name': 'api', 'method': 'GET', 'url': api_url, 'headers': headers, 'parse': 'json'}]
    out = []
    for withref in (False, True):
        h = dict(headers)
        if withref:
            h['Referer'] = '{{page_url}}'
        st = json.loads(json.dumps(steps))
        st[1]['headers'] = h
        out.append({'id': 'wpjson' + ('_ref' if withref else ''), 'pattern_id': '', 'tier': 'T0',
                    'origin': 'skill:json_api_html_content', 'steps': st,
                    'output': {'title': {'step': 'api', 'path': tpath}, 'main_text': main_text, 'links': links}})
    return out
