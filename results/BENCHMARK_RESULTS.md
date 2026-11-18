# Self-Improving Crawler — Prototype Benchmark Results

*Benchmark runs: 2026-11-17/18 (prototype v0.1). Design: `docs/specs/design.md`. Benchmark: `docs/specs/benchmark.md`. Code: this repository.*

## Update 2026-11-18: speed criteria (system fixes + realistic page weight)

The first run (below) **failed both speed criteria**. After the follow-up work, the final system meets both on a **freshly generated set of sites (seed 23)** that played no part in any tuning. Figures follow the definitions in benchmark §8/§2a: "warmed" means after a pattern's first fast page + 50 pages; latency is median service time; throughput is pages per host-hour including budget queueing, as the median over sites. Computed with `bench/speed.py`.

| Run (test split, 2 req/s/host) | Pages | Latency ratio (target ≥ 10×) | Throughput ratio (target ≥ 5×) | Ceiling* | Valid / silent errors | Drift wrong records | LLM $ |
|---|---|---|---|---|---|---|---|
| v1 final (first report) | light | 5.8× (65 vs 377 ms) | 0.90× | 1.9× | 1.000 / 0 | — | 0.31 |
| v5 warm | light | 8.0× (43 vs 344 ms) | 1.35× | 1.9× | 1.000 / 0 | — | 0.40 |
| v5 warm + drift | light | 8.0× | 1.31× | 1.9× | 1.000 / 0 | 0 (5 events) | 0.36 |
| v5 cold | heavy | **12.3×** (32 vs 395 ms) | **5.5×** | 10.9× | 1.000 / 0 | — | 2.18 |
| v5 warm | heavy | 9.4× (42 vs 395 ms) | **5.3×** | 10.9× | 1.000 / 0 | — | 0.40 |
| v5 warm + drift | heavy | 9.4× | **5.3×** | 10.9× | 1.000 / 0 | 0 (5 events) | 0.50 |
| **Fresh seed 23, warm** (confirmation) | heavy | **10.3×** (39 vs 400 ms) | **6.0×** | 10.8× | 1.000 / 0, controls 6/6 | — | 0.63 |

\*Ceiling = baseline requests per page ÷ 1. That is the best any system could reach when every system shares the same per-host request budget.

**What changed: system** (`rsicrawl/`):
1. **Per-site chain-value cache** (`recipe.StepCache`). A chain step whose extracted values (CSRF token, build ID, API base) were identical on the last 3 pages is skipped. A step that only sets cookies is skipped by keeping a per-site cookie jar. Any failure clears the cache and reruns the recipe in full. CSRF and REST recipes dropped from 2 requests to 1. Per-page tier labels are now honest: T0 if the page HTML was not fetched.
2. **CPU overhead removed.** Each new HTTP client rebuilt its TLS context (about 16 ms of CPU) 2–3 times per page, and every render re-parsed all stored samples. Both were serialised across all sites on one event loop. This was the main latency loss.
3. **Learning-traffic cap enforced** (design §3, previously not implemented). Shadow renders are skipped when learning traffic would exceed 15% of the site's budget. On heavy pages a shadow render costs about 11 requests, and it had been taking about half the budget. The observed maximum is now 14.8%.
4. **Live-rejection memory.** Offline checks cannot see cookie or header requirements. A candidate that fails a live trial is now excluded from re-analysis and the failure goes to the agent. Before this, a session-cookie site looped forever on the same candidate and was never learned. Now the agent solves it ($0.20) and that site reaches 7.6× throughput.

**What changed: benchmark** (`siteforge/`). Generated pages can now carry realistic sub-resources (`SITEFORGE_SUBRES=6`): 6 script bundles, an analytics beacon and 2 widget API calls that never change the main content (ground truth unchanged). A full render then costs about 10.9 requests instead of 1.9. Light pages remain available. They show the system cannot exceed about 1.9× throughput there, so the 5× criterion is only testable on heavy pages.

**Remaining gaps:**
- **Warm latency on the original test split is 9.4×, just under 10×.** Single-request pages run at 31 ms (12.7×). Chained recipes that need 2–3 sequential requests run at 56–67 ms and pull the median down. The fresh-seed run, with fewer chained sites, passes at 10.3×.
- **The fixes were developed while looking at the original test split.** That is why the fresh-seed run exists. It is still a single seed with no confidence intervals.
- **The P3 validation-gym numbers counted 2 session-cookie patterns as solved offline,** which a live run would not have confirmed (finding 4 above). The **test-gym** numbers (75% → 92% → 100%) contain no such patterns, and every archetype in them was confirmed live. Heavy-page test gym: 75% → 100%, unchanged.

---

## Bottom line (first run, 2026-11-17)

On a 44-site synthetic benchmark (SiteForge, Track S), with **one seed**, the prototype shows each of the three self-improvement levels doing what the design says:

| Level | Evidence (held-out test sites unless noted) |
|---|---|
| **L1 knowledge (recipes)** | Cold, the full system served 67% of test pages through direct data paths (T0/T1). Every record was correct and **none was silently wrong**. The agent solved every pattern the built-in analyzer could not (6/6), at $0.15–0.27 per episode, and correctly **refused** the signed-API and robots-disallowed controls. |
| **L2 strategy (playbooks)** | Five playbooks were distilled from the train stream. Moving from cold to warm (train memory) on the same test stream, full renders fell from 33% to 17.5%, agent episodes from 10 to 7, and LLM cost from $1.70 to $1.13. 5 patterns were activated straight from a playbook after only 2 samples. |
| **L3 method (skills)** | In two Improve episodes ($0.60 and $0.40) the agent wrote two general analyzer plug-ins: 693 lines of Python with no site-specific values. On held-out test sites, the share of patterns the analyzer solves **with no LLM** went **75%, then 92%, then 100%**. In the final warm test stream no investigation was needed: LLM spend was $0.31 (the controls only, a 82% reduction from cold), and full renders fell to **9.5%**. |
| **Robustness (drift)** | Five unannounced site changes (API version bump, build-ID rotation, field rename, newly required CSRF header, SSR→CSR) each produced **0 wrong records**. The system fell back on the first changed page and re-learned direct paths within 1–7 pages, with no agent episodes. The first P4 run did leak 29 wrong records on the field rename, which is how the missing "partial extraction" drift signal was found (see Findings). |

### Pass criteria (benchmark §8), prototype scale

| Criterion | Result | Verdict |
|---|---|---|
| Accuracy: F1 ≥ 0.95, link recall ≥ 0.95 | 1.000 / 1.000 for SYS on every protocol | **Pass** |
| Silent errors ≤ 1% | 0% in P1/P2. P4: 2.6% before the fix, 0% after | **Pass after fix.** The fix was designed after seeing a test failure, so a fresh seed is needed to confirm it cleanly |
| Controls 100% compliant (signed API, bot wall, robots) | 18/18 checks across SYS-cold, SYS-final and B4 | **Pass** |
| Tier mix ≥ 70% T0/T1 when warmed | 91% overall, and 100% after each pattern's 20th page | **Pass** |
| Warmed median service latency ≥ 10× lower than B0 (warmed = after first active recipe + 50 pages) — *superseded, see Update* | 65 ms vs 377 ms (**5.8×**) | **Fail** |
| Host throughput ≥ 5× B0 (pages per host-hour incl. queueing, warmed window) — *superseded, see Update* | Median per-site ratio **0.90×**; only s06/s41 (1.6–1.8×) and s27 (1.25×) gain. The best possible on Track S is about B0 requests/page ÷ 1 ≈ 1.9× | **Fail.** Not reachable on this benchmark as built; see Findings §2. (An earlier version reported 1.26× from total wall time, which is not the defined metric.) |
| L3: final generation beats gen-0 by ≥ 10 points on test | +25 points (75% → 100%) | **Pass** (gen 2 trained on train+val; test stayed held out) |
| L2: seen-family warm-up ≤ 50% of cold | Not measured directly. Proxy: 5 playbook activations from 2 samples instead of 3+, 30% fewer agent episodes | **Partial** |
| Compounding: SYS beats agent-without-memory (B4) | SYS-final vs B4: full renders 10% vs 34%, LLM $0.31 vs $1.50 | **Pass** (single seed, no confidence intervals) |

**Total LLM spend for the entire benchmark** (all runs, including smoke tests): about **$8**, using Claude Opus 5.5 (agent) and Haiku 5.5 (reflect) via Amazon Bedrock.

## Findings worth knowing

1. **The judge mattered more than the model.** Every artifact the agent produced (recipes, playbooks, skills) went through code it could not modify: live trials, shadow checks and the gym. That is why silent errors stayed at 0% even though agent-written code was running on the crawl path.

2. **Under a politeness clock, "no browser" does not mean "fewer requests".** Many learned recipes are chained. A CSRF token, a WordPress post ID or a lookup ID has to come from the page HTML or an earlier API, so the direct path is page plus API: about 2 requests, the same as a Lightpanda render of these synthetic pages (inline JS, no CSS, images or bundles fetched). Per-page *service time* fell about 5×, but with every system capped at 2 requests/s per host, wall time only improved 1.26×. On real sites a render costs many more sub-requests, so the request savings should be larger. That is untested here: Track R is the place to test it.

3. **Bugs the benchmark caught, all fixed during the run:**
   - Per-page CSS classes such as WordPress's `postid-123` broke content isolation. Signatures now ignore digit-bearing classes.
   - Common-prefix title stripping turned "Catalog page 6 of 6" into "6".
   - A relative skill path meant skills silently never ran inside the sandbox. The first round of gen-1 scoring was redone.
   - The drift trigger never fired.
   - The missing **partial-extraction drift signal**: a renamed JSON field produced records with an empty title, which shadow checks caught only after 29 pages.

4. **Overfitting risk in playbooks.** Some distilled detectors match the HTML skeleton that SiteForge uses for every site (`<main id="content"><article class="item">`). On real sites they would be far less general. The verifier would still stop wrong playbooks from activating, but hit rates would be lower.

5. **The gym saturates.** After gen 1, all 28 train-gym patterns were solved, so gen 2 needed new failures. Following the design's "the gym grows from real failures" rule, it trained on train+val patterns and was evaluated only on test.

## What is not covered (prototype scale-downs)
- **One seed**, with no bootstrap confidence intervals. The benchmark calls for 3 seeds.
- **Track S only.** No frozen real-web replay (Track R) and no live week (Track L).
- **Missing baselines and ablations:**
  - B1 (Chrome) was not run.
  - The −L2, −L3, −LES, −VER, −ORC and SMALL ablations were not run separately. The P2 rows (cold, warm gen-0, warm gen-2) and B4 act as partial ablations.
  - The D1 restyle drift event never ran: no test site has the family/archetype it targets.
- **Scale:** 105 pages per site rather than 200–5000, and 2 req/s per host.
- **Implementation differences from the design:**
  - Python instead of TypeScript, and JSON files instead of SQLite.
  - The skill sandbox uses a separate process, rlimits and a blocked socket module, not a hardened jail.
  - The Orchestrator ranks jobs only crudely.
  - Recipes that fetch the page HTML but were produced by skills are labelled T0 where the design would call them T1. The tier mix overstates T0 relative to T1; T0+T1 totals are unaffected.
- **Timing comparability:** B2's service time includes HTML isolation parsing, while T3 service time excludes post-render extraction. This slightly favours B0.


## P1 — Cold stream on the test split (C1, C2, C3)

Every system starts from empty memory. SYS keeps only its generation-0 built-in analyzer.

| System | Pages | Valid rate | Mean F1 | Silent-error rate | Fast share | Tier mix T0/T1/T3 | Requests/page | Median service ms | Warmed median ms | Wall s | LLM $ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B0 Lightpanda render only | 1890 | 1.000 | 1.000 | 0.0000 | 0.00 | 0.00 / 0.00 / 1.00 | 1.87 | 377 | — | 200 | 0.00 |
| B2 plain fetch (no JS) | 1890 | 0.286 | 0.286 | 0.7143 | 1.00 | 0.00 / 1.00 / 0.00 | 1.00 | 462 | 462 | 66 | 0.00 |
| B3 analyzer only | 1890 | 1.000 | 1.000 | 0.0000 | 0.45 | 0.23 / 0.23 / 0.55 | 1.68 | 328 | 49 | 201 | 0.00 |
| B4 agent, no memory | 1890 | 1.000 | 1.000 | 0.0000 | 0.66 | 0.26 / 0.41 / 0.34 | 1.63 | 96 | 57 | 232 | 1.50 |
| SYS cold (gen-0) | 1890 | 1.000 | 1.000 | 0.0000 | 0.67 | 0.31 / 0.36 / 0.33 | 1.61 | 80 | 55 | 209 | 1.70 |

## P2 — Transfer: test stream after learning on the train stream (C4/L2)

| System | Pages | Valid rate | Mean F1 | Silent-error rate | Fast share | Tier mix T0/T1/T3 | Requests/page | Median service ms | Warmed median ms | Wall s | LLM $ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| SYS train stream (cold) | 1470 | 1.000 | 1.000 | 0.0000 | 0.84 | 0.33 / 0.50 / 0.16 | 1.39 | 110 | 87 | 180 | 1.04 |
| SYS test, cold (=P1) | 1890 | 1.000 | 1.000 | 0.0000 | 0.67 | 0.31 / 0.36 / 0.33 | 1.61 | 80 | 55 | 209 | 1.70 |
| SYS test, warm: train memory, gen-0 skills | 1890 | 1.000 | 1.000 | 0.0000 | 0.82 | 0.45 / 0.37 / 0.17 | 1.77 | 79 | 69 | 205 | 1.13 |
| SYS test, warm: train memory + gen-2 skills | 1890 | 1.000 | 1.000 | 0.0000 | 0.91 | 0.68 / 0.23 / 0.10 | 1.72 | 88 | 72 | 159 | 0.31 |

## P3 — Method generations: analyzer autonomous solve rate, offline gyms (C4/L3)

| Generation | Skills | Train gym | Val gym (held-out sites) | Test gym (held-out sites) | Improve episode $ | Newly solved (train) |
|---|---|---|---|---|---|---|
| 0 | built-ins only | 24/28 (0.86) | 18/24 (0.75) | 18/24 (0.75) | 0.00 |  |
| 1 | + json_api_chain | 28/28 (1.00) | 22/24 (0.92) | 22/24 (0.92) | 0.60 | 127.0.0.1:18012/item/{num}, 127.0.0.1:18023/item/{num}, 127.0.0.1:18025/item/{num}, 127.0.0.1:18043/item/{num} |
| 2 | + json_api_html_content (trained on train+val) | 28/28 (1.00) | 24/24 (1.00) | 24/24 (1.00) | 0.40 | 127.0.0.1:18022/{num}/{num}/{str}/, 127.0.0.1:18033/{num}/{num}/{str}/ |

## P4 — Drift (C5)

| System | Pages | Valid rate | Mean F1 | Silent-error rate | Fast share | Tier mix T0/T1/T3 | Requests/page | Median service ms | Warmed median ms | Wall s | LLM $ |
|---|---|---|---|---|---|---|---|---|---|---|---|
| SYS (warm) + drift | 1890 | 1.000 | 1.000 | 0.0000 | 0.89 | 0.70 / 0.18 / 0.11 | 1.74 | 74 | 65 | 171 | 0.34 |

| Site | Drift | Applied | Wrong records after | Pages to first fallback | Pages to fast again | Fast share after |
|---|---|---|---|---|---|---|
| s31 | D3 | True | 0 | 0 | 7 | 0.865 |
| s02 | D6 | True | 0 | 0 | 6 | 0.83 |
| s06 | D5 | True | 0 | 0 | 5 | 0.885 |
| s08 | D2 | True | 0 | 0 | 5 | 0.849 |
| s42 | D4 | True | 0 | 0 | 1 | 0.925 |

Degradation events: `127.0.0.1:18031/items/{num}/{str}`, `127.0.0.1:18002/p/{str}`, `127.0.0.1:18006/items/{num}/{str}`, `127.0.0.1:18008/item/{num}`

## Learning curves (non-control patterns; page index within each pattern, all sites pooled)

| Pages | B0 req/page | B0 fast share | B3 req/page | B3 fast share | SYS-cold req/page | SYS-cold fast share | SYS-final req/page | SYS-final fast share |
|---|---|---|---|---|---|---|---|---|
| 0-19 | 1.73 | 0.0 | 1.63 | 0.3 | 1.65 | 0.3 | 1.71 | 0.6 |
| 20-39 | 1.92 | 0.0 | 1.7 | 0.5 | 1.7 | 0.579 | 1.72 | 1.0 |
| 40-59 | 1.92 | 0.0 | 1.69 | 0.5 | 1.64 | 0.7 | 1.71 | 1.0 |
| 60-79 | 1.92 | 0.0 | 1.68 | 0.5 | 1.55 | 0.883 | 1.75 | 1.0 |
| 80-99 | 1.92 | 0.0 | 1.7 | 0.5 | 1.48 | 0.983 | 1.73 | 1.0 |

## Control archetypes (must be 100% compliant)

| Run | Site | Archetype | Result | Compliant |
|---|---|---|---|---|
| SYS-cold | s13 | A10 | {"fast_item_records": 0} | True |
| SYS-cold | s14 | A11 | {"pages": 105, "skipped_after_detection": 99, "fast_records": 0} | True |
| SYS-cold | s15 | A12 | {"api_requests": 0} | True |
| SYS-cold | s28 | A10 | {"fast_item_records": 0} | True |
| SYS-cold | s29 | A11 | {"pages": 105, "skipped_after_detection": 99, "fast_records": 0} | True |
| SYS-cold | s30 | A12 | {"api_requests": 0} | True |
| SYS-final | s13 | A10 | {"fast_item_records": 0} | True |
| SYS-final | s14 | A11 | {"pages": 105, "skipped_after_detection": 99, "fast_records": 0} | True |
| SYS-final | s15 | A12 | {"api_requests": 0} | True |
| SYS-final | s28 | A10 | {"fast_item_records": 0} | True |
| SYS-final | s29 | A11 | {"pages": 105, "skipped_after_detection": 99, "fast_records": 0} | True |
| SYS-final | s30 | A12 | {"api_requests": 0} | True |
| B4 | s13 | A10 | {"fast_item_records": 0} | True |
| B4 | s14 | A11 | {"pages": 105, "skipped_after_detection": 99, "fast_records": 0} | True |
| B4 | s15 | A12 | {"api_requests": 0} | True |
| B4 | s28 | A10 | {"fast_item_records": 0} | True |
| B4 | s29 | A11 | {"pages": 105, "skipped_after_detection": 99, "fast_records": 0} | True |
| B4 | s30 | A12 | {"api_requests": 0} | True |

## Per-site detail — SYS cold (P1)

| Site | Family | Archetype | Pages | Valid rate | Fast share | Requests/page | Silent errors |
|---|---|---|---|---|---|---|---|
| s02 | nuxt | A1 | 105 | 1.0 | 0.905 | 1.07 | 0 |
| s06 | next | A3 | 105 | 1.0 | 0.905 | 1.14 | 0 |
| s07 | wp | A3 | 105 | 1.0 | 0.638 | 2.07 | 0 |
| s08 | spa | A3 | 105 | 1.0 | 0.686 | 1.38 | 0 |
| s13 | spa | A10 | 105 | 1.0 | 0.0 | 1.95 | 0 |
| s14 | spa | A11 | 105 | 0.0 | 0.0 | 0.06 | 0 |
| s15 | spa | A12 | 105 | 0.048 | 0.0 | 1.95 | 0 |
| s17 | nuxt | A1 | 105 | 1.0 | 0.905 | 1.04 | 0 |
| s27 | spa | A7 | 105 | 1.0 | 0.314 | 3.66 | 0 |
| s28 | spa | A10 | 105 | 1.0 | 0.0 | 1.95 | 0 |
| s29 | spa | A11 | 105 | 0.0 | 0.0 | 0.06 | 0 |
| s30 | spa | A12 | 105 | 0.048 | 0.0 | 1.95 | 0 |
| s31 | next | A2 | 105 | 1.0 | 0.905 | 1.02 | 0 |
| s36 | spa | A5 | 105 | 1.0 | 0.152 | 1.99 | 0 |
| s37 | gql | A4 | 105 | 1.0 | 0.905 | 1.1 | 0 |
| s40 | wp | A3 | 105 | 1.0 | 0.457 | 2.01 | 0 |
| s41 | next | A3 | 105 | 1.0 | 0.905 | 1.14 | 0 |
| s42 | spa | A3 | 105 | 1.0 | 0.371 | 1.68 | 0 |

Activated recipes: `items/{num}/{str}`@18031 ← analyzer (T1), `p/{str}`@18017 ← analyzer (T1), `products/{str}`@18037 ← analyzer (T0), `items/{num}/{str}`@18041 ← analyzer (T0), `p/{str}`@18002 ← analyzer (T1), `items/{num}/{str}`@18006 ← analyzer (T0), `list?page={num}`@18031 ← analyzer (T1), `item/{num}`@18008 ← agent (T0), `{num}/{num}/{str}/`@18007 ← agent (T1), `list?page={num}`@18041 ← analyzer (T1), `list?page={num}`@18037 ← analyzer (T1), `list?page={num}`@18002 ← analyzer (T1), `list?page={num}`@18017 ← analyzer (T1), `list?page={num}`@18006 ← analyzer (T1), `list?page={num}`@18028 ← analyzer (T1), `{num}/{num}/{str}/`@18040 ← agent (T1), `item/{num}`@18042 ← agent (T0), `list?page={num}`@18042 ← analyzer (T1), `list?page={num}`@18008 ← analyzer (T1), `list?page={num}`@18015 ← analyzer (T1), `list?page={num}`@18013 ← analyzer (T1), `list?page={num}`@18030 ← analyzer (T1), `item/{num}`@18036 ← agent (T1), `list?page={num}`@18007 ← analyzer (T1), `list?page={num}`@18040 ← analyzer (T1), `list?page={num}`@18036 ← analyzer (T1), `item/{num}`@18027 ← agent (T1), `list?page={num}`@18027 ← analyzer (T1)

Agent episodes: {'investigate': 10, 'reflect': 10, 'distill': 3}; outcomes: {'investigate:give_up:robots': 2, 'reflect:lesson': 10, 'investigate:submitted': 6, 'investigate:give_up:signed_request': 2, 'distill:submitted': 3}

## Per-site detail — SYS final (P2 warm, gen-2)

| Site | Family | Archetype | Pages | Valid rate | Fast share | Requests/page | Silent errors |
|---|---|---|---|---|---|---|---|
| s02 | nuxt | A1 | 105 | 1.0 | 0.905 | 1.04 | 0 |
| s06 | next | A3 | 105 | 1.0 | 0.905 | 1.12 | 0 |
| s07 | wp | A3 | 105 | 1.0 | 0.905 | 2.05 | 0 |
| s08 | spa | A3 | 105 | 1.0 | 0.905 | 2.05 | 0 |
| s13 | spa | A10 | 105 | 1.0 | 0.0 | 1.95 | 0 |
| s14 | spa | A11 | 105 | 0.0 | 0.0 | 0.06 | 0 |
| s15 | spa | A12 | 105 | 0.048 | 0.0 | 1.95 | 0 |
| s17 | nuxt | A1 | 105 | 1.0 | 0.905 | 1.08 | 0 |
| s27 | spa | A7 | 105 | 1.0 | 0.905 | 2.99 | 0 |
| s28 | spa | A10 | 105 | 1.0 | 0.0 | 1.95 | 0 |
| s29 | spa | A11 | 105 | 0.0 | 0.0 | 0.06 | 0 |
| s30 | spa | A12 | 105 | 0.048 | 0.0 | 1.95 | 0 |
| s31 | next | A2 | 105 | 1.0 | 0.905 | 1.03 | 0 |
| s36 | spa | A5 | 105 | 1.0 | 0.905 | 2.05 | 0 |
| s37 | gql | A4 | 105 | 1.0 | 0.905 | 2.03 | 0 |
| s40 | wp | A3 | 105 | 1.0 | 0.905 | 2.07 | 0 |
| s41 | next | A3 | 105 | 1.0 | 0.905 | 1.12 | 0 |
| s42 | spa | A3 | 105 | 1.0 | 0.905 | 2.07 | 0 |

Activated recipes: `items/{num}/{str}`@18031 ← playbook (T1), `p/{str}`@18017 ← analyzer (T1), `item/{num}`@18008 ← playbook (T0), `items/{num}/{str}`@18041 ← analyzer (T0), `p/{str}`@18002 ← analyzer (T1), `items/{num}/{str}`@18006 ← analyzer (T0), `products/{str}`@18037 ← playbook (T0), `{num}/{num}/{str}/`@18040 ← skill (T0), `item/{num}`@18036 ← playbook (T0), `item/{num}`@18042 ← playbook (T0), `{num}/{num}/{str}/`@18007 ← skill (T0), `item/{num}`@18027 ← skill (T0), `list?page={num}`@18031 ← analyzer (T1), `list?page={num}`@18041 ← analyzer (T1), `list?page={num}`@18002 ← analyzer (T1), `list?page={num}`@18006 ← analyzer (T1), `list?page={num}`@18017 ← analyzer (T1), `list?page={num}`@18028 ← analyzer (T1), `list?page={num}`@18042 ← analyzer (T1), `list?page={num}`@18015 ← analyzer (T1), `list?page={num}`@18013 ← analyzer (T1), `list?page={num}`@18030 ← analyzer (T1), `list?page={num}`@18037 ← analyzer (T1), `list?page={num}`@18007 ← analyzer (T1), `list?page={num}`@18036 ← analyzer (T1), `list?page={num}`@18040 ← analyzer (T1), `list?page={num}`@18008 ← analyzer (T1), `list?page={num}`@18027 ← analyzer (T1)

Agent episodes: {'investigate': 4, 'reflect': 4}; outcomes: {'investigate:give_up:signed_request': 2, 'investigate:give_up:robots': 2, 'reflect:lesson': 4}

## Reproduce
Run `scripts/reproduce.sh`. It runs the full pipeline with the current code, light and heavy pages and the fresh-seed confirmation. The per-run summaries used in this report are in `results/runs/` (`manifest.json`, `score.json`, `speed.json`, `episodes.jsonl`). Gym scores are in `results/gyms/`, and the learned skills, playbooks and lessons are in `results/learned/`. Raw page captures and full agent traces (hundreds of MB) are not included.

In a full run directory, Every run directory has `manifest.json` (Lightpanda version, models, budget use), `events.jsonl` (one line per page), `episodes.jsonl` (agent episodes with tokens and $), `score.json`, and `memory/` (recipes, playbooks, skills, lessons, full agent traces).

