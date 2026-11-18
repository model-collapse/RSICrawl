# Lightpanda Headless Browser — A Study

*Date: 2026-11-17. Binary tested: `1.1.0-nightly.12226+88fb36db0` (x86_64 Linux). Source: `lightpanda-io/browser` @ `74a93e16` (main, 2026-11-17).*
*Test host: AWS, 128 vCPU, 246 GB RAM, idle. Comparison: Chrome headless-shell (Playwright build 1234) driven by the same puppeteer-core 25.17 scripts.*

---

## 1. What it is

Lightpanda is a headless browser **written from scratch in Zig**. It is not a Chromium, WebKit or Gecko fork. It is aimed at scraping, automation and AI agents. It deliberately leaves out the most expensive part of a browser, **layout and painting**, and keeps the part automation needs: HTTP, an HTML5 parser, a full DOM, V8 JavaScript and the Web APIs.

| Component | Choice |
|---|---|
| Language | Zig 0.17 (~319k lines in 702 `.zig` files), plus a Rust staticlib |
| JS engine | V8, through Lightpanda's `zig-v8-fork`, with a startup snapshot embedded in the binary |
| HTML parser | html5ever (Servo), streaming, through a C FFI sink |
| Network | libcurl multi with BoringSSL, nghttp2 (HTTP/2), brotli, zstd and zlib |
| Storage | SQLite (IndexedDB backend, HTTP cache) |
| Text "rendering" | Rust: parley + skrifa + tiny-skia, with bundled DejaVu fonts |
| LLM client (agent mode) | `zenai` (Lightpanda's own) |
| Binary | single 78 MB file; dynamically linked only to libc and libm (glibc ≥ 2.35) |
| License | AGPL-3.0 (CLA required to contribute) |

### Commands
- `fetch <url>…` — one-shot load and dump. Dump formats: `html`, `markdown`, `semantic_tree`, `semantic_tree_text`, `structured_data`, `png`, `pdf`, `wpt`.
- `serve` — CDP WebSocket server (Puppeteer/Playwright via `connect`), plus optional WebDriver BiDi and classic WebDriver.
- `mcp` — MCP server over stdio or HTTP, with per-session isolation.
- `agent` — built-in LLM agent REPL (Anthropic, OpenAI, Gemini, Vertex, Mistral, OpenRouter, Ollama, …).
- `run <script.js>` — replays a deterministic "PandaScript" recorded from an agent session, with no LLM.

---

## 2. Architecture (from source)

### 2.1 Object model
```
App ── V8 Platform, Snapshot, Network (shared conn pool/DNS/robots/CORS), Telemetry, ArenaPool
 └─ Browser (1 per thread; owns 1 V8 isolate + HttpClient)
     └─ Session (exactly one; cookies, storage, IndexedDB, history, worker registries)
         └─ Page (root Frame + iframes; page arena, DOM Factory, JS identity map)
             └─ Frame (one document/window; ~4.8k LOC)
```
V8 isolates are thread-affine, so **each CDP connection gets its own OS thread, Browser and isolate** (`server/Server.zig` `spawnWorker`). Within a worker everything runs on one thread: `Runner._tick` interleaves V8 macro/microtasks, the timer scheduler, `curl_multi` polling and the CDP inbox. A main epoll loop accepts sockets and frames WebSocket messages. Each worker is the only writer to its socket, so the only lock is on the inbox.

### 2.2 Page load pipeline
1. `Frame.navigate` sends a libcurl transfer. A header callback handles MIME and charset sniffing.
2. The body streams into an **incremental html5ever parser**. Its callbacks build Zig DOM nodes, and a preload scanner prefetches scripts.
3. `ScriptManager` keeps queues for classic, async, defer and module scripts and handles import maps. Parser-blocking scripts spin synchronously, bounded by a deadline.
4. Lifecycle events run in order: `interactive`, `DOMContentLoaded`, `load`, `complete`. Network-idle notifications follow.
5. **Wait goals are first-class:** `domcontentloaded | load | networkidle | networkalmostidle | settled | done`, plus `--wait-selector`, `--wait-script` and `--wait-ms`.

### 2.3 DOM and memory
- **Nodes are small.** Each one holds parent, first-child, next and prev pointers (the first child's `prev` points to the last child), a u8 type, flags and a u32 owner index. Rarely used per-node state goes in page-level side tables, and Attr nodes are created lazily.
- **Inheritance is one contiguous allocation.** For example, EventTarget→Node→Element→HTMLElement→HTMLDivElement is a single block, and casting between them uses comptime offset arithmetic.
- **Allocators:** pooled, size-bucketed arenas (`ArenaPool`); a custom single-threaded arena, which is ~10% faster than Zig's now thread-safe std arena; and a `RecyclingAllocator` with size-class free lists. Arenas kept alive by V8 objects are reported to V8 as external memory, so the GC sees them.
- **Bindings are generated at compile time.** Each Web API type declares a `JsApi` struct (functions, accessors, indexed and named properties), and `bridge.zig` flattens ~470 imports into the binding table. All function templates go into a **V8 startup snapshot** that is `@embedFile`d, which makes startup nearly instant.

### 2.4 What "headless without rendering" actually means
This is the most important thing to understand about Lightpanda:

| Area | Status |
|---|---|
| CSS cascade | **Partial.** `StyleManager` tracks only the properties used by the "renderless layout": display, visibility, opacity, pointer-events, overflow, width, height, position, white-space, text-transform. Other properties come back from `getComputedStyle` as initial or UA values (verified: `color` returns black when the stylesheet sets red). |
| External stylesheets | **Not fetched by default** in `fetch`/`serve`; on by default in `mcp`/`agent`. |
| Geometry | Estimated (`layout.zig` plus fake deterministic text metrics), so `getBoundingClientRect` returns plausible numbers. Real only for explicit sizes. |
| Screenshot / PDF | **Text-only reflow** of the page's content blocks (headings, paragraphs, lists) drawn with tiny-skia, not a picture of the page. In testing, RTL text was handled but CJK glyphs came out as boxes (no CJK font bundled). |
| Canvas 2D | API exists, but **53 drawing calls are no-ops**. |
| WebGL | `getContext('webgl')` returns `null` on purpose. |
| Images / iframes / workers / stylesheets | Opt-in through `--load-resources`. Images are never decoded; only the first 16 KB is read for dimensions. |
| Workers | Dedicated and Shared workers run as separate V8 contexts **on the same thread**. ServiceWorker is experimental and behind a flag. |
| WebSocket, EventSource, Fetch, XHR, IndexedDB, CacheStorage, BroadcastChannel, Web Locks | Implemented |

### 2.5 Protocol surface
- **CDP:** 21 standard domains (Page 20 methods, Runtime 22, DOM 18, Network 18, Fetch interception, Input, Target 14, Accessibility, Debugger 32, Emulation (partly stubbed), …). Two custom domains:
  - **`LP.*`**: getMarkdown, getSemanticTree, getInteractiveElements, detectForms, clickNode, fillNode, getPageStatus, …
  - **`WebMCP.*`**: backs `navigator.modelContext`.
- **Hard limit: one BrowserContext per CDP connection** (`target.zig`: *"Cannot have more than one browser context at a time"*). To run in parallel, open more connections; each gets its own thread.
- **WebDriver BiDi:** session, script, browsingContext, input, storage and a little emulation. Also ~47 classic WebDriver HTTP routes.

### 2.6 AI-oriented features
- **Semantic tree** (`SemanticTree.zig`): a pruned accessibility tree (role, accessible name, value, interactivity flag, XPath, stable backendNodeId). Its text form prioritises interactive and focused nodes when truncating. This is the main "page → LLM context" format.
- **MCP server:** 40 tools — goto, search, markdown, html, screenshot, links, evaluate, extract, tree, nodeDetails, interactiveElements, structuredData, detectForms, click, fill, scroll, waitForSelector, waitForScript, waitForState, hover, press, selectOption, setChecked, findElement, consoleLogs, getUrl, getCookies, getEnv, popups, download, pageTools, callPageTool, classify, acceptCookies, login, save, run, session_new, session_list, session_close. Over HTTP, each `Mcp-Session-Id` gets its own worker thread and browser.
- **PandaScript:** plain JS running in a *separate* isolate, with one global `Page` whose methods mirror the tools. An agent session records to a script, and secrets are scrubbed into `$LP_*` placeholders. The script then replays with no LLM.
- **Page health:** `PageStatus` reports JS errors, unsupported APIs hit (webgl, canvas2d), ReferenceErrors on missing globals, and detected bot challenges.

### 2.7 Telemetry (on by default)
- **What:** batched NDJSON from a background thread to `https://telemetry.lightpanda.io/v2`. Each batch carries a persisted install UUID, mode, OS, arch and version. Events: `run`, `navigate` (TLS on/off and type, **no URL**), `llm` (provider and model name), `tool` (tool id, source, outcome, duration) and `mcp_client` (client family, e.g. `claude_code`).
- **Opt out:** set `LIGHTPANDA_DISABLE_TELEMETRY=true`. Every test in this study ran with it set.

### 2.8 Code health signals
- 98 TODO/FIXMEs in total. About 217 "unimplemented/stub/noop" hits, mostly in `webapi` (190, mainly canvas).
- Known CDP gaps: some Emulation and Browser methods are no-ops, `Network.*ExtraInfo` events are missing, and there is no box model for `display:none` elements.
- Testing: a custom test runner that **fails any test that leaks memory**, a daily Web Platform Tests run (results at perf.lightpanda.io/wpt), and end-to-end tests with Puppeteer and Playwright.

---

## 3. Hands-on results

### 3.1 Web API compatibility probe (55 checks, local page, via CDP)
Lightpanda gave the **same result as Chrome on 49 of 55 checks**: querySelectorAll, custom elements, shadow DOM, MutationObserver, Intersection/ResizeObserver, rAF, fetch, sync XHR, local/sessionStorage, indexedDB, cookies, crypto.randomUUID, crypto.subtle.digest, Intl, history.pushState, DOMParser, structuredClone, Blob/FileReader, matchMedia, dynamic `import()`, Range/Selection, click dispatch, FormData, form validity, elementFromPoint, ES2023/2024 built-ins, …

The 6 checks that differ (5 below, plus the encoding case after the table):

| Check | Lightpanda | Chrome | Note |
|---|---|---|---|
| `getComputedStyle(el).color` (stylesheet sets red) | `rgb(0, 0, 0)` | `rgb(255, 0, 0)` | partial cascade (§2.4) |
| WebGL context | `false` | `true` | intentional |
| `navigator.serviceWorker` | `undefined` | object | experimental flag |
| `navigator.userAgent` | `Lightpanda/1.0` | Chrome UA | configurable with `--user-agent` |
| `navigator.webdriver` | `false` | `true` | |

There is also an encoding difference. When a page is served without a charset, Lightpanda assumes UTF-8, while Chrome falls back to windows-1252. In this test Lightpanda's result was the one the author intended.

### 3.2 Real-world sites (30 URLs, sequential, `waitUntil: load`, new context per page)
| | Lightpanda | Chrome headless-shell |
|---|---|---|
| Success | 30/30 | 30/30 |
| Total time | **10.3 s** | 14.2 s |
| Peak memory (PSS) | **122 MB** | 458 MB |

- **DOM fidelity is high.** Element counts after load matched Chrome exactly on 20 of 30 sites and were within ~8% on the rest, covering Wikipedia, HN, GitHub, React/Vue/Angular/Svelte/Next.js sites, BBC, The Guardian, PyPI and others.
- **Network dominates real-world wall time**, so the speed-up here is only ~1.4×. One reason is that Chrome also downloads images and CSS while Lightpanda skips them.
- **Bot walls** (Reddit, Stack Overflow, Amazon) returned the same tiny challenge pages to *both* engines.
- **XHR-driven pages need a later wait condition.** On `campfire-commerce`, `fetch` with the default wait dumped "Loading…". `--wait-until networkidle` produced the full content in 0.19 s at 24 MB RSS.

### 3.3 Local throughput (network removed, 200 page loads, local HTTP server)
Pages: `static.html` is a 3000-row table; `js.html` builds 5000 nodes from JSON, adds listeners, queries them and calls `fetch()`.

| Page | Concurrency | Lightpanda pages/s | Chrome pages/s | Lightpanda memory | Chrome memory |
|---|---|---|---|---|---|
| static | 1 | **41** | 6.6 | 29 MB | 481 MB (RSS) |
| static | 8 | **211** | 40 | 67 MB | 1.4 GB (RSS) |
| js | 1 | **32** | 7.2 | 49 MB | 265 MB (PSS) |
| js | 8 | **157** | 47 | 106 MB | 606 MB (PSS) |
| js, reused tab | 8 | **169** | 66 | 199 MB | 8.5 GB (PSS)* |

\*In the reused-tab mode, Chrome's renderer grew without bound on this page. This was not investigated further. Lightpanda also grows in reuse mode (49→110 MB at concurrency 1), so creating a fresh context per page is the better pattern for both.

**Takeaway:** for CPU-bound, DOM- and JS-heavy work, Lightpanda is **~3–6× faster and uses ~5–10× less memory** on this hardware. Lightpanda's README claims 9× faster and 16× lighter on an m5.large. Those figures are plausible on a smaller box, where Chrome's multi-process overhead hurts more, but they were not reproduced here. Part of Chrome's per-page cost is creating and destroying a browser context, which Lightpanda makes nearly free.

### 3.4 Other features exercised
- `fetch --dump markdown` on example.com: 0.06 s, 25 MB.
- `--dump semantic_tree_text` produced a compact `[i] link 'title'` outline of HN that is well suited to an LLM. `--dump structured_data` returned OpenGraph, Twitter card, meta tags and microdata as JSON.
- `lightpanda run script.js` with `page.extract({quotes: [".quote .text"]})` on a JS-rendered quotes site: 0.6 s, 33 MB RSS, clean JSON.
- The MCP stdio server answered `initialize`, `tools/list` (40 tools), `goto` and `interactiveElements` correctly.

---

## 4. Assessment

### Strengths
- **Efficiency:** around 25–50 MB per page and fast startup (snapshot). Contexts are cheap enough that you can run many more parallel crawlers per machine than with Chrome.
- **Real JS and DOM compatibility:** it runs V8 and passes most common Web API checks, so React, Vue and Next.js pages produce the same DOM as in Chrome.
- **Drop-in CDP:** existing Puppeteer scripts worked unchanged through `connect()`, as far as this study exercised them.
- **AI-native extras:** markdown and semantic-tree dumps, a built-in MCP server with session isolation, and record/replay PandaScript. These are features, not add-ons.
- **Single static-ish binary:** no Chrome dependency tree, so Docker images are small.

### Limitations and risks
- **No visual fidelity.** Screenshots are text reflow, canvas drawing does nothing, and there is no WebGL. Anything that depends on pixels, visual regression testing, or canvas fingerprinting will fail or be wrong.
- **Partial CSS.** Logic that depends on computed styles beyond display, visibility and size may behave differently, as can layout-dependent JS (virtualised lists, scroll-position math, "is element in viewport" heuristics).
- **Distinct fingerprint.** The UA, `navigator.webdriver = false`, no WebGL and fake text metrics make it easy for anti-bot systems to identify. It is not a stealth tool.
- **CDP coverage is not complete.** There is one BrowserContext per connection, some Emulation and Network events are stubbed, and Playwright features beyond the basics may hit gaps.
- **Maturity.** Releases are nightlies (v1.1.0-nightly) and the codebase is large and moving fast.
- **Governance.** The AGPL license and default-on telemetry may matter for embedding in commercial products.

### When to use it
| Use Lightpanda | Use Chrome |
|---|---|
| Large-scale crawling or scraping of JS-rendered pages | Screenshots, PDFs that must look like the page, visual testing |
| LLM agents that need page text, markdown or an a11y tree | Canvas-, WebGL- or layout-dependent sites |
| Cost- or memory-constrained parallel automation | Sites with aggressive anti-bot fingerprinting |
| Form filling, clicking, extracting structured data | Full CDP/Playwright feature surface, extensions |

A pragmatic pattern is **Lightpanda first, Chrome as fallback**. The `LP.getPageStatus` "unsupported API / bot challenge" signals are designed to trigger exactly that fallback.

---

## Appendix: reproducing
Artifacts were produced on the study machine (not included in this repository):
- `bin/lightpanda` — the nightly binary
- `lightpanda-src/browser/` — the source clone
- `bench/bench.mjs` — real-site benchmark; `bench/benchpss.mjs` is the PSS variant. Run `node bench.mjs <lightpanda|chrome> urls.txt load`.
- `bench/bench2.mjs` — local throughput; `bench/bench2pss.mjs` is the PSS variant. Run `[REUSE=1] node bench2.mjs <engine> <url> <N> <concurrency>`.
- `bench/probe.mjs` + `bench/site/probe.html` — the API compatibility probe
- `bench/site/` — static, JS and probe pages. Serve them with `python3 -m http.server 8765`.
- `bench/res-*.json[l]`, `bench/probe-*.json` — raw results

The memory samplers (`rss_sampler.sh`, `pss_sampler.sh`) sum memory over the whole process tree every 50 ms. RSS overcounts Chrome's shared pages, so prefer the PSS figures when comparing.
