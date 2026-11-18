# Self-Improving Crawler — Benchmark Design

*Companion to `docs/specs/design.md` (the "main design"). Status: draft for review · 2026-11-17*

## 1. What the benchmark must prove

The main design claims five things. The benchmark has to be able to show each one, or show that it is false:

| # | Claim | Evidence the benchmark produces |
|---|---|---|
| C1 | **Faster and more successful** than rendering every page | cost and success compared with browser-only baselines on the same pages |
| C2 | **Correct.** The fast path never trades accuracy for speed | accuracy against ground truth, plus the **silent-error rate** |
| C3 | **Self-improving within a site.** Cost falls as pages are crawled | learning curves, the break-even point |
| C4 | **Self-improving across sites (L2) and in method (L3).** Each new site is cheaper to learn | per-new-pattern learning cost across the stream; gym generations |
| C5 | **Robust to change.** Drift is detected and repaired without serving wrong data | drift scenarios with known change times |

Each level's contribution is isolated by **ablations** (§6). A claim counts as shown only if the full system beats the relevant ablation by more than the run-to-run noise (§9).

## 2. Design principles
- **Reproducible first.** The headline results come from offline tracks, where the web is frozen or synthetic. A small live track checks that the results hold on the real web.
- **Realistic network costs.** The replayed network includes the latencies and bandwidth actually recorded, because the Lightpanda study showed network time dominates on real sites.
- **The benchmark is a stream, not a fixed set.** Self-improvement only shows up over time, so the system processes pages in a fixed seeded order and is measured as it goes.
- **No leakage.** Held-out sites, ground truth and scoring are invisible to the agent, and they run in a separate process from the system under test.
- **The same judge as production where possible**, plus exact ground truth where it is available (the synthetic track).

## 2a. Stream rules (how crawling and learning share time)
- **Fixed page list.**
  - Each pattern's pages come from a URL list fixed when the site is forged (S) or recorded (R). Every system processes exactly the same pages in the same seeded order.
  - Links a system discovers are scored for recall, but they don't change the page list. Link discovery, including A8 listings, is measured as a metric, not by letting each system crawl a different set.
- **Politeness is the clock.**
  - Each system crawls as fast as one shared **per-host request budget** allows: by default **2 requests/s per host**. Every request counts, including sub-requests during renders, T0 calls, shadow checks and agent experiments.
  - There is also a fixed global concurrency (32 workers by default).
  - There is no artificial arrival rate. A full render costs tens of requests and a T0 page costs one, so a system that learns sooner finishes a host sooner. The agent's thinking time competes with the crawl's real progress, just as it would in production.
- **No deferral.**
  - Each host's pages are served in list order, each with whatever artifacts are active *at that moment*.
  - A system may not hold back or skip pages to wait for a recipe.
  - All learning is asynchronous and charged to total cost.
- **Latency is reported two ways:**
  - **service latency**, from dispatch to record, excluding time spent waiting for the budget;
  - **host throughput**, in pages per host-hour, which includes queueing.

## 2b. System-under-test contract
To make baselines and ablations possible, the system under test exposes:
- **Toggles in one config file** for:
  - memory stores (recipes, playbooks, skills, lessons) on/off/frozen;
  - Verifier mode (live shadow / activation-only);
  - Orchestrator mode (value-ranked / FIFO);
  - per-job model override;
  - agent on/off;
  - L3 Improve on/off;
  - the approval policy for network-behaviour skills.
- **A per-page event stream** (JSONL): page id, tier, service time, queue time, requests, bytes, record, and the artifact ids used.
- **A per-episode event stream:** job type, tokens, dollars, requests and outcome.
- **A run-snapshot command** that freezes and exports all memory stores. P2 and P3 use it to carry state between streams.

**Build order:** the harness and the baselines that need no system (B0, B1, B2) are built first, against a mock system that honours the contract. They are then integrated as the system matures.

## 3. Tracks

### 3.1 Track S — Synthetic sites ("SiteForge")
**Purpose:** exact ground truth, controlled difficulty, scheduled drift, and exactly reproducible server behaviour, including header and token checks that a replay can't reproduce.

**What it is:** a local generator of complete websites. Each generated site has:
- a site *family* (framework style);
- an *archetype* (where the data comes from and what it takes to get it);
- 2–4 URL patterns of 200–5,000 pages each;
- deterministic content from a seed, with ground-truth records written to a file the system under test can't access.

**Families**, used to test playbook transfer: Next.js-like (`__NEXT_DATA__`, `/_next/data/{build}/…json`), Nuxt-like (`window.__NUXT__`), WordPress-like (`/wp-json/wp/v2/…`), Shopify-like (`/products/{handle}.json`), generic SPA (REST), GraphQL app, classic server-rendered.

**Archetypes**, ordered by difficulty:

| Code | Data source / obstacle | Best achievable tier |
|---|---|---|
| A1 | static server-rendered HTML | T1 |
| A2 | SSR with embedded JSON state | T1 |
| A3 | client-rendered from a REST call keyed by a URL variable | T0 |
| A4 | GraphQL query (inline or persisted-query hash) | T0 |
| A5 | REST call needing a header or token taken from the HTML (CSRF / build id) | T0 (chained) |
| A6 | data call needing a cookie set by an earlier response | T0 (chained) |
| A7 | multi-step chain (config → id lookup → data) | T0 (chained) |
| A8 | paginated or infinite-scroll listings (link discovery) | T0/T1 |
| A9 | A/B template variants on one URL pattern | T1/T0, with a split pattern |
| A10 | **request signed by client-computed HMAC** (control: unsolvable in v1) | T2 |
| A11 | **bot wall / CAPTCHA** (control: must be detected and skipped, never "solved") | none |
| A12 | robots.txt disallows the API path (control: must not call it) | T1/T2 |

**Network emulation:** each site gets a latency/bandwidth profile drawn from the distribution recorded on the live track. The default is a median of 120 ms RTT with a long tail. The profile is applied identically to every system under test.

**Scale:**
- **Core set:** about 120 sites (7 families × 12 archetypes, minus combinations that make no sense, plus seed repeats), about 300k pages. It is split as in §4.
- **Drift set:** an **additional** 60 sites (6 events × 10) generated only for P4.
- **Novel-family set:** one family per seed, generated in addition to the core set and used only for test.

In total that is about 200 sites and 400k pages, which sits within the main design's v1 scale.

**Ground truth on S** is the **displayed main content**: exactly the text, fields and per-page links the generator put into the page's variable regions. It is *not* any system's render. Every system, B0 included, is scored against it, so content-isolation errors in full renders show up as well.

### 3.2 Track R — Frozen real web ("Replay")
**Purpose:** real-world diversity under reproducible conditions.

- **Recording:**
  - Select about 50 public sites, stratified across the archetypes above (as observed) and across sectors: news, e-commerce, docs, forums, listings, government, blogs. Use 2–3 URL patterns per site and about **500 pages per pattern**, roughly 60k pages in total.
  - Record every request with a full render through a **recording proxy** (WARC/HAR with exact bodies and timings), honouring robots.txt and politeness rules. Also record a second render of a 10% sample a few minutes later, to measure natural nondeterminism.
  - The recording includes every XHR/API call the pages make. So a T0 request that reproduces one of them is a replay **hit**.
- **Replay:** a **replay proxy** serves the recording to every system under test (Lightpanda via `--http-proxy`, Chrome, and Node `fetch`).
  - Requests are matched on normalised method + URL + body. Normalisation strips known cache-busters and timestamp parameters; the rules are fixed per site at record time.
  - Recorded timings are replayed per response.
  - **Misses** return 504 and are logged. A miss counts as a failed request for the system, so it can't invent requests that never happened.
- **Known limitation:** replay can't model server reactions to *new* header or parameter combinations, such as header ablation. Those behaviours are tested on Track S. On Track R, ablation probes are answered "as recorded if the request matches, otherwise a miss", and the report says so.
- **Reference records** come from a **Chrome (B1) full render** of the recording, with the main design's content isolation. Chrome is used rather than Lightpanda so that B0 is not its own reference. On R, the metric is reported as **"agreement with full-render reference"**.
- **Absolute accuracy** is estimated for every system from a **5% sample that a human labels**. The labeller marks the true main text and per-page links on the recorded page. Correctness claims (C2) rest mainly on Track S ground truth, with Track R's human-labelled sample as confirmation.

### 3.3 Track L — Live check ("Field")
**Purpose:** confirm the offline results transfer to the real web, including drift that happens naturally.

- About 15 real sites, crawled politely, with the system running continuously for 7 days on a fixed budget of about 20k pages per day.
- Accuracy is measured with same-session T3 shadow renders on a 5% sample.
- Results are reported separately and never mixed into the headline offline scores.

## 4. Data splits (no leakage)
- **Track S:** split by **site** into **train (60%) / validation (20%) / test (20%)**. Every family appears in all three splits. In addition, one entire family (chosen per seed) is **held out of train and validation**, to measure transfer to a *novel* family.
- **Track R:** split by site 50/20/30. The agent never sees test-site captures, and the gym is built only from train captures.
- **Allowed uses:**
  - Skills and playbooks may be developed, and gym generations run (§5.3), **only on train**.
  - Validation is used to pick thresholds.
  - Test is run **once per reported result**.
- **Learning loops on validation and test streams:**
  - L1 (Investigate, Repair), Reflect and L2 Distill run **as in production**.
  - L3 Improve is **disabled**.
  - The gym is a **read-only snapshot of train**: nothing from validation or test captures enters it, and playbook gates use only its train split.
- **"Empty memory" in P1** means no recipes, playbooks or lessons. The skill library is the **generation-0 built-in Analyzer plug-ins**, because those are part of the system as built.
- **Network-behaviour skills** are auto-**rejected** during benchmark runs, keeping the guardrail and allowing repetition. The number rejected is reported. An optional ablation, **+NETSKILL** (auto-approve), measures what the guardrail costs.

## 5. Protocols

### 5.1 P1 — Cold stream (C1, C2, C3)
1. Start with empty memory: no recipes, playbooks, skills or lessons.
2. The system crawls the test split as one interleaved stream. Sites arrive in seeded order, and within each site, pages arrive in seeded order through the normal frontier.
3. Learning is on.
4. Everything is measured continuously.

This shows the within-site learning curve and the break-even point.

### 5.2 P2 — Transfer (C4, L2)
1. Run the train stream to completion, building recipes, playbooks and lessons.
2. Freeze L3 (no new skills).
3. Run the test stream with L1/L2 learning on.
4. Compare with P1 on the same test stream.

The difference is the value of accumulated cross-site knowledge. Results are reported separately for **seen families** and the **novel family**.

### 5.3 P3 — Method generations (C4, L3)
1. Generation 0 is the system as built.
2. For each generation g:
   - **(a)** run the train stream;
   - **(b)** let the Orchestrator run Improve and Distill jobs on what was accumulated, up to a fixed agent budget per generation;
   - **(c)** freeze the resulting skills and playbooks as generation g+1;
   - **(d)** evaluate generation g+1 on **validation**, with P1 rules (cold L1 memory, keeping only L2/L3 artifacts).
3. Run 5 generations. Evaluate test only on the final generation and on generation 0.

The main curve is the **Analyzer's autonomous solve rate** across generations, together with agent tokens per solved pattern. Self-improvement in method means the first rises and the second falls without accuracy dropping.

### 5.4 P4 — Drift (C5)
On Track S, each drift event happens at a known page index in the stream, and the generated site changes without warning:

| Event | Change |
|---|---|
| D1 | the template is restyled, but the data source is unchanged (should not trigger a re-learn) |
| D2 | the API path or version changes (`/api/v1` → `/api/v2`) |
| D3 | a JSON field is renamed |
| D4 | a token or header requirement is added |
| D5 | the build id rotates (Next.js-like) |
| D6 | the site switches architecture (SSR → CSR) |

**Measures:**
- detection latency (pages from the event to demotion);
- **wrong records served** between the event and demotion;
- repair latency (pages or time until a new active recipe);
- extra agent cost per event.

Each event is run on at least 10 sites.

### 5.5 P5 — Throughput at scale (C1)
Run the warm system (after P2) on the full Track S test split at fixed concurrency (8, 32 and 128 workers), on fixed hardware. Report pages per second, CPU-seconds per page, peak memory and per-host request rate. The memory figure follows the study's method: PSS summed over the process tree.

## 6. Systems compared

**Baselines**

| ID | System | Represents |
|---|---|---|
| B0 | Lightpanda full render for every page (T3 only) | "just use a fast headless browser" |
| B1 | Chrome headless-shell full render for every page | the industry default |
| B2 | Plain HTTP fetch + Readability-style main-content extraction (no JS) | cheap lower bound; expected to fail on A3–A7 |
| B3 | Algorithmic Analyzer only, no agent (approach B) | the value of having an agent at all |
| B4 | Agent investigates every pattern from scratch: no playbooks, skills or lessons; recipes kept (approach A) | the value of compounding memory |
| **SYS** | Full system (approach C) | — |

**Ablations of SYS**

| ID | Removed | Tests |
|---|---|---|
| −L2 | playbooks | cross-site strategy |
| −L3 | skills (frozen at generation 0) | method improvement |
| −LES | lessons | episodic memory |
| −VER | live shadow verification (offline check at activation only) | the value of the Verifier; expect a higher silent-error rate |
| −ORC | Orchestrator value ranking (FIFO jobs instead) | the value of prioritisation |
| SMALL | Haiku 5.5 for every job | sensitivity to model size |

All systems use identical hardware, concurrency, per-host rate limits, robots policy and network emulation.

## 7. Metrics

### 7.1 Correctness (scored by the harness against ground truth: exact on S, T3 reference on R)
- **Record accuracy:**
  - `main_text` token F1
  - per-page **link recall/precision**
  - `title` exact match after normalisation
  - `fields` exact-match rate (S only, where the field ground truth is exact)
- **Valid-record rate:** the share of pages yielding a record that passes equivalence.
- **Silent-error rate (primary safety metric):** the share of records *served by a fast tier* that fail equivalence. It is reported overall, per tier, and during drift windows.
- **Control compliance** must be 100%:
  - A10 never yields an invented "signed" recipe;
  - A11 is detected and skipped;
  - A12's API is never called.

### 7.2 Efficiency
- **Latency per page:** median and p95.
- **Throughput:** pages per second at fixed concurrency.
- **Requests and bytes per page** sent to target hosts, which also measures politeness.
- **CPU-seconds per page and peak PSS.**
- **LLM tokens and dollars per 1,000 pages**, split into crawl-time (always 0 by design) and learning-time.
- **Total cost per page:** wall time, LLM dollars and compute dollars, each **reported separately**, plus one combined figure using published conversion rates fixed in the benchmark config (so it can be recomputed).

### 7.3 Learning (computed over the stream, in page-index units per pattern)
- **Learning curve:** total cost per page against pages processed, in a sliding window, overall and for each site.
- **AULC:** area under the learning curve, normalised to B0. Lower is better. This is the single number summarising C3.
- **Break-even point (per pattern):** the page index after which SYS's *cumulative* cost (including all learning cost attributed to that pattern) drops below B0's cumulative cost. It is reported per pattern, as a median, and as the share of patterns that ever break even.
- **Samples-to-active:** T3 renders a pattern needs before its first active recipe.
- **Agent cost per solved pattern:** tokens and episodes.
- **Autonomous solve rate:** the share of new patterns solved by the Analyzer with no agent episode (P3 curve).
- **Tier mix over time:** the share of pages served by T0, T1, T2 and T3.

### 7.4 Drift (P4)
- detection latency
- wrong records served
- repair latency
- extra agent cost
- false re-learns on D1

## 8. Pass criteria (v1)
These come from the main design's targets, with the protocol stated explicitly:

| Criterion | Protocol | Threshold |
|---|---|---|
| Accuracy | P1 & P2, S and R test | `main_text` F1 ≥ 0.95 mean; link recall ≥ 0.95 |
| Silent errors | P1, P2, P4 | ≤ 1% of fast-tier records overall; ≤ 20 wrong records per drift event (median) |
| Controls | all | 100% compliance |
| Speed (C1) | P2, S and R test | on **warmed patterns**, i.e. pages after a pattern's first active recipe + 50 pages: median service latency ≥ 10× lower than B0 **and** host throughput ≥ 5× B0; tier mix ≥ 70% T0/T1 on archetypes that are not controls |
| Within-site learning (C3) | P1, **S** (gated); R reported | median break-even ≤ 300 pages per pattern; AULC better than B0 and B3 |
| Cross-site (C4, L2) | P2 vs P1 | seen-family samples-to-active ≤ 50% of cold; novel family no worse than cold |
| Method (C4, L3) | P3 | autonomous solve rate increases monotonically over at least 3 of 5 generations, and the final generation beats generation 0 on test by ≥ 10 points, with no accuracy drop |
| Compounding | ablations | SYS beats −L2, −L3 and B4 on AULC beyond the noise band (§9) |

## 9. Statistical rigour and reproducibility
- **Seeds:** every configuration is run with **3 seeds**, which vary the stream order and the Track S content. Results are mean ± 95% CI from a bootstrap over sites. A difference is claimed only if the confidence intervals don't overlap.
- **LLM non-determinism:**
  - Reported results always use fresh LLM calls.
  - For debugging and regression, an **LLM record/replay cache** (prompt hash → response) makes a rerun of the same commit and seed fully deterministic.
  - Model ids and versions are pinned in the run manifest.
- **Run manifest:** the commit, configuration, model ids, seeds, recording ids, hardware and the Lightpanda/Chrome versions. Each run's report embeds its manifest.
- **Reference noise:** the Track R double-render sample gives the natural F1 between two full renders of the same page. That number is the noise floor, and accuracy claims are interpreted relative to it.
- **Isolation:**
  - The harness (proxies, ground truth, scorer) runs as a separate process and user.
  - The system under test gets only the proxy endpoint and its own data directory.
  - Ground-truth files and test captures are not readable by it.

## 10. Harness components
- **`siteforge`:** the synthetic site generator and server (families × archetypes × seeds), with drift scheduling and network emulation. It writes ground truth to a harness-only directory.
- **`recorder` / `replayer`:** a record and replay proxy (WARC/HAR) with per-site normalisation rules, recorded-timing playback and miss logging. *Builder's call:* mitmproxy with custom addons or a purpose-built proxy.
- **`runner`:** runs a system configuration (baselines, SYS, ablations) through a protocol on a track. It enforces identical limits and collects per-page events (tier, latency, bytes, requests, tokens, record).
- **`scorer`:** computes §7 from the events and ground truth. It is versioned, and the system under test can't modify it.
- **`report`:** a scorecard against §8, plus the curves:
  - learning curve per system;
  - tier mix over time;
  - P3 generations;
  - drift timelines;
  - cost breakdown.

  Output is static HTML plus JSON.
- **CLI:**
  ```
  bench forge   --seed S
  bench record  --sites r.txt
  bench run     --system SYS|B0..B4|ablation --track S|R|L --protocol P1..P5 --seed S
  bench report  --runs <ids>
  ```

## 11. Cost and runtime estimate (rough, for planning)
- **Track S:** P1, P2 and P4 for SYS plus 5 baselines and 6 ablations, × 3 seeds, at about 300k pages each. Offline runs are bounded by the per-host request budget (§2a). B0 and B1 are slowest, at roughly 10 s of budget per rendered page per host, so they take on the order of a day per run. SYS finishes faster as it learns.
- **The LLM** is the main dollar cost, and it applies only to SYS, B4 and the ablations. Cap it per run in the config.
- **P3:** 5 generations × train stream + Improve budget. This is the most expensive protocol, so run it with 1 seed during development and 3 for the reported result.
- **Track R recording:** a one-time polite crawl of about 60k pages (plus a 10% second render).

## 12. Calls I made (please check)
- **Stream rules (§2a):** a fixed page list, the per-host request budget (2 req/s/host) as the clock, no deferral, and latency reported both as service latency and as host throughput.
- **References:** generator ground truth on S. On R, a Chrome full-render reference plus a 5% human-labelled sample.
- **Test-time learning:** L1, L2 and Reflect are on, L3 is off, and the gym is frozen from train. Network-behaviour skills are auto-rejected during runs.
- **Scale:** sets are added on top of the core set for drift and the novel family. R uses 500 pages per pattern.
- **System contract and build order (§2b):** the harness and B0–B2 come first, against a mock system.
- **Three tracks:** synthetic for exact truth and drift, frozen real for diversity, a small live track for validity. Headline numbers come from the offline tracks only.
- **The benchmark is a seeded stream**, with AULC and break-even as the summary measures of self-improvement.
- **Baselines:** Lightpanda-only, Chrome-only, plain fetch, algorithms-only, and agent-without-memory, plus six ablations.
- **Silent-error rate** is the primary safety metric, with 100% compliance required on the three control archetypes (signed API, bot wall, robots-disallowed API).
- **Scale:** about 200 synthetic sites / 400k pages (core + drift + novel family), about 50 recorded real sites / 60k pages, 15 live sites for 7 days. 3 seeds and bootstrap confidence intervals.
- **Pass thresholds** in §8 are my proposal, aligned with the main design's targets.

## 13. Left to the builder
- the exact site lists for Track R and Track L;
- the generator internals;
- the proxy implementation;
- URL normalisation rules per site;
- the event schema;
- report styling;
- the conversion rates for the combined cost figure.
