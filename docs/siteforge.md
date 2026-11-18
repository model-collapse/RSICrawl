# SiteForge

Synthetic benchmark websites (harness side). The crawler must not import this package or read
`truth/` or `server_data/`.

```
cd RSICrawl
python -m siteforge.generate --seed 7 --out runs/forge7 [--sites-per-combo 1] [--items 120]
python -m siteforge.server --forge runs/forge7 --control-port 18999
python -m siteforge.selftest [--no-drift]     # generates seed 7, serves it, renders in Lightpanda
```

Files: `common.py` (URL shapes, truth text), `generate.py`, `shapes.py` (per-family JSON shapes and
drift-dependent API paths), `pages.py` (HTML + inline ES2020 JS), `sha256js.py` (plain-JS SHA-256 for
A10), `server.py` (one aiohttp app per site plus the control app in one event loop), `selftest.py`.

Combos: 15 base combos plus 7 repeats, multiplied by `--sites-per-combo`. Site ids are `s01..`, and
ports start at 18001 in combo order. Splits: controls (A10/A11/A12) go to test. Within each family,
the other sites are shuffled with the seed and assigned train, test, val, train, ...

## Payload shapes (state before drift; names after D3 are in brackets)
- next: `__NEXT_DATA__` = `{props:{pageProps:{item}}, page, query:{id,slug}, buildId, isFallback}`.
  On A3 the pageProps object is empty and `isFallback` is true. `GET /_next/data/{buildId}/items/{id}.json`
  returns `{pageProps:{item}}`. `item = {id, slug, title[headline], body:[p], specs:{price,sku,category,rating}, related:[{id,slug,title[headline]}]}`.
- nuxt: `window.__NUXT__={data:{"product-<id>": item}, ...}`. The API is `GET /api/v1/products/{id}` and is only used
  by pages after D6. `item = {id, slug, title[name], content:[p], specs:[{label,value}], related:[{id,slug,title[name]}]}`.
- wp: A2 embeds schema.org Product JSON-LD `{name, description[articleBody] (paragraphs joined by "\n\n"), sku, category,
  offers.price, aggregateRating.ratingValue, url, isRelatedTo:[{name,url}]}`. A3 sets `<body data-post-id=N>`.
  `GET /wp-json/wp/v2/posts/{N}` returns `{id, date, slug, link, title:{rendered}, content:{rendered:"<p>..</p>"},
  meta[acf]:{price,...}, related:[{id,title:{rendered},link}]}`.
- spa (A3/A5/A6/A10/A12): `GET /api/v1/items/{id}` returns `{item:{id, slug, title[name], paragraphs:[p], price, sku,
  category, rating, related:[{id,title[name]}]}}`. A5 needs the `X-CSRF-Token` meta value. A6 needs a `sid` cookie
  from `GET /api/session`. A10 needs `?ts=<unix s>&sig=sha256hex(SECRET+id+ts)`, where SECRET is an inline `var SECRET="..."`.
- spa A7: the page has `window.__PAGE__={slug}`. `GET /api/config` returns `{apiBase:"/svc/v3"}`. Then
  `{apiBase}/lookup?slug=` returns `{internalId, slug}`, and `{apiBase}/records/{internalId}` returns
  `{record:{internalId, label[heading], text:"p\n\np", attrs:{price,...}, links:[{id,label[heading]}]}}`.
- gql: `POST /graphql` returns `{data:{product:{id, slug, name, description:"p\n\np", attributes[specs]:[{key,value}],
  related:[{slug,name}]}}}`.

## Drift (`POST /drift` returns `{"applied": bool, ...}`)
- D1: toggles class names and adds a wrapper. This applies to all sites except A11.
- D2: bumps the API version (`/api/vN`, `/svc/vN`, `/wp-json/wp/vN`, `/api/vN/products`, `/vN/graphql`).
  The old path returns 404. It applies only to sites that are in CSR mode now and are not next.
- D3: renames one field, as shown in brackets above. It applies to sites in embed or CSR mode.
- D4: makes the CSRF header required and embeds the meta tag from then on. It applies to sites in CSR mode
  that do not already require the header.
- D5: rotates the next build id from a pre-generated list. The old id returns 404. It applies to all next sites.
- D6: switches SSR or embedded-state item pages to a CSR shell that uses the family API. It applies to
  next, nuxt and wp sites with A1 or A2.
- A11 never applies drift. Inline JS is generated per request from the current state, so a real
  browser render always matches truth.

## Deviations / clarifications
- The generator also writes `server_data/<site_id>.json` (the full item data, secrets and build ids)
  for the server. This is harness-only.
- A11 truth records keep the real title, main_text, fields and links, with `blocked:true`. A11
  returns 403 for every path except `/robots.txt`.
- List pages: the `<h1>`/title is `Catalog page N of M`. `main_text` is the h1 plus the item titles
  (one per line). `fields` is `{}`.
- Family APIs are served whether or not the page uses them (for example, `/_next/data` on next A1/A2,
  and `/wp-json` on wp A2). They are just not referenced in the HTML.
- GraphQL returns the full product object whatever the selection set. D3 is enforced by rejecting
  the old field name with a GraphQL error.
- Extra control endpoints: `GET /state` and `GET /health`. `GET /log` without `site_id` returns all
  sites, and `since=<t>` filters by time.
- Cookies are set on `127.0.0.1`. HTTP clients must accept cookies for IP hosts, for example
  aiohttp `CookieJar(unsafe=True)`.
