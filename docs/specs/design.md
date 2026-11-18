# Self-Improving Crawler — Design

*Status: draft for review · 2026-11-17*

## 1. Intent

A general-purpose web crawler that **improves itself**. The longer it runs, the faster and more reliable it gets. It also gets *better at getting better*: each new site costs it less effort to learn than the last.

**The opportunity.** Pages under one URL pattern (for example `shop.com/product/{id}`) share a template, and their main content comes from one data source. That source might be a JSON API the page calls, JSON embedded in the HTML, or the server-rendered HTML itself. Rendering every page in a browser repeats the same discovery millions of times. Once the system knows *where the data comes from and how to request it*, it can skip the browser.

**The self-improvement.** A crawl-engineering **agent** studies the crawler's rich logs and improves the system at three levels. Each level compounds on the one below:

| Level | What improves | Artifact | Effect |
|---|---|---|---|
| **L1 — Knowledge** | how to get one pattern's data directly | **Recipe** | that pattern's pages get faster and more reliable |
| **L2 — Strategy** | how a *kind* of site works (framework, CMS, API style) | **Playbook** | new sites of that kind are learned in a few samples, often with no LLM |
| **L3 — Method** | how the system itself analyses pages, and how the agent works | **Skill** (analyzer plug-in, tool macro, procedure) | problems the agent once solved by hand are solved automatically from then on |

The key dynamic is that **the agent progressively makes itself unnecessary.** Anything it solves repeatedly is compiled down: into a playbook (L2), or into deterministic analyzer code (L3). Over time the LLM handles only genuinely new problems, so both crawl cost and learning cost per page fall continuously.

Every improvement at every level must pass an **independent, unchangeable judge** before it takes effect: live verification against full renders for recipes, and an offline benchmark ("the gym") for playbooks and skills.

## 2. Goals, non-goals, anti-goals

**Goals**
- Works on any site, with no per-site configuration.
- Measurable improvement at all three levels over time (§8).
- Fast-path output is **equivalent** to full-render output for the main content and the per-page links.
- Every recipe, playbook and skill is traceable: the evidence it came from, the episode that produced it, how it was judged, and how it performs now. It can also be rolled back.

**Non-goals (v1)**
- Bypassing CAPTCHAs or anti-bot systems, and crawling behind logins.
- Running across multiple machines (v1 is one machine; the design must not block scale-out later).
- Visual rendering or screenshots.
- Deciding *what* to crawl, beyond a basic frontier.

**Anti-goals** (failure even if it "works")
- **Silently wrong or partial data.** Correctness always outranks speed.
- **Gaming the judge.** The agent must never be able to change, or see the answers of, whatever evaluates it (§7.5).
- **Making the system hit a host harder** than a plain crawl would.
- **A knowledge base or skill library that only grows.** Stale or useless artifacts are demoted and retired automatically.
- **Running agent-written code outside a sandbox.**

## 3. Constraints
- **Slow-path browser:** Lightpanda over CDP via `puppeteer-core`, as verified in `docs/lightpanda-study.md`. It provides network events with response bodies, markdown, a semantic tree, `LP.getPageStatus` (bot-challenge and unsupported-API signals) and `networkidle` waits. Chrome headless-shell is the fallback when Lightpanda reports unsupported APIs. Lightpanda's telemetry is disabled.
- **Crawl etiquette:** robots.txt is obeyed, the user agent is identifiable, and every tier shares **one per-host rate budget**. The agent's experiments, shadow checks and verification all draw from that budget. Learning traffic is capped at about 15% of it, so learning never raises the load on a host above a plain crawl.
- **Scale (v1):** one machine, on the order of 1 million pages across about 1,000 sites.
- **Capture retention:** keep full captures for each artifact's provenance, the latest ~20 per pattern, every failed check, and the gym corpus (§7.4). Passing shadow checks keep only a summary.

## 4. Approaches considered
- **A. The LLM analyses every pattern.** Flexible, but the cost never falls and nothing compounds.
- **B. Purely algorithmic induction.** Cheap, but it can't handle the long tail and can't improve itself.
- **C. Hybrid, self-improving — CHOSEN by the user.** Deterministic algorithms handle the common case. The agent handles what the algorithms can't, then pushes its solutions down into playbooks and analyzer skills, so the algorithmic share keeps growing.

## 5. System overview

```
 ┌──────────────────────────── CRAWL PLANE (deterministic, fast) ────────────────────────────┐
 │ Frontier ─► Pattern Classifier ─► Router ─┬─ T0 direct data request ─┐                    │
 │    ▲                                      ├─ T1 HTML + extractor ────┤                    │
 │    │                                      ├─ T2 Lightpanda, minimal ─┼─► Record ─► output │
 │    └──────────── per-page links ◄─────────┴─ T3 full render+capture ─┘                    │
 └───────────────────────────────────────────────────┬────────────────────────────────────────┘
                     captures, failures, metrics     │            ▲ recipes / playbooks / skills
 ┌───────────────────────────── LEARNING PLANE ──────▼────────────┴───────────────────────────┐
 │ Analyzer (algorithmic + skill plug-ins) ── candidate ──►  JUDGES (immutable)                │
 │        │ can't solve                                     ├ Verifier (live, vs T3)           │
 │        ▼                                                 └ Gym (offline benchmark)          │
 │ Orchestrator ─ jobs ─► AGENT ─ tools ─► Capture store / Lightpanda / HTTP (budgeted)        │
 │  (code, ranks by value)  │                                                                  │
 │                          └─ writes ─► Memory: Recipes · Playbooks · Skills · Lessons ·      │
 │                                        Episode traces                                       │
 └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

**The crawl plane** never calls an LLM. **The learning plane** runs alongside it, consuming the crawl plane's logs and publishing improvements to it.

## 6. Crawl plane

### 6.1 Tiers

| Tier | What it does | Cost |
|---|---|---|
| **T0** | Direct request to the data source (JSON API, GraphQL, `/_next/data`, `.json` variant), then a declarative extractor | one small HTTP request |
| **T1** | Plain GET of the page, then extraction from embedded JSON (`__NEXT_DATA__`, JSON-LD, `window.__STATE__`) or CSS selectors | one HTML request, no JS |
| **T2** | Lightpanda render that blocks all requests except those the recipe marks as needed | a light browser render |
| **T3** | Full Lightpanda render (Chrome as fallback) with complete capture | slowest; produces the logs the system learns from |

The router uses the fastest **active** recipe for the pattern, or T3 if there is none. On a fast-path failure it falls back one tier and records the failure, which is a learning signal.

### 6.2 Pattern Classifier
- **URL templates:** URL tokens are split into variable positions (numeric, slug, hash, UUID) and constant ones, giving a `site + template` such as `/product/{num}/{slug}`.
- **Template fingerprint:** a hash of the DOM skeleton from T3 renders. When one URL template yields two distinct skeletons, it is split into two patterns.
- **Unknown URLs** go to T3. The classifier re-clusters periodically.

### 6.3 T3 capture
- final DOM, markdown and semantic tree;
- every network request and response (headers, bodies with a size cap, timing);
- cookies;
- `LP.getPageStatus`.

### 6.4 Recipes (L1 artifact)
A recipe is **declarative data**, not code:
- request template(s) and variable bindings;
- required headers and cookie names;
- extractor rules (JSONPath, CSS, or a regex for embedded JSON);
- link rules;
- provenance.

v1 primitives for dynamic values are chained values (taken from an earlier response, cookie or the HTML), timestamps, and URL/base64 encoding. Cryptographic request signing is not supported in v1, so signed APIs stay on T2. *The exact DSL syntax is the builder's call.*

### 6.5 Output record and equivalence
- **Record fields:**
  - `title`
  - `main_text`: the content that varies across a pattern's pages, i.e. everything except template boilerplate
  - `fields`: structured, best-effort
  - `links`: **per-page** links (in the main content, plus pagination), and **site-constant** links (navigation, collected once per pattern)
- **Equivalence with T3:**
  - `main_text` token F1 ≥ 0.9
  - per-page link recall ≥ 0.95
  - `title` matches
  - every `fields` value the recipe returns matches the reference

  These thresholds are starting values.

## 7. Learning plane

### 7.1 Analyzer (deterministic first pass)
Runs per pattern after N T3 samples:
1. **Content isolation:** diff the DOMs across samples. What varies is main content, which becomes the reference record.
2. **Source attribution:** search the main-content snippets in every captured body (JSON values, inline script JSON, raw HTML), and pick the smallest set of sources that covers ≥ the threshold.
3. **Request derivation:** express each source request as a template over URL variables or values from earlier responses. Find which headers are required by bounded ablation.
4. **Extractor synthesis**, then output a **recipe candidate** for the Verifier.

The Analyzer is **extensible**. Each step runs a pipeline of **skill plug-ins**: attributors, request-derivers, extractors and framework detectors. The agent adds to this pipeline at L3 (§7.3.4). That is how solutions are pushed down into deterministic code.

### 7.2 Orchestrator (code, not LLM)
Turns signals into agent **jobs** and ranks them by expected value:

```
value(job) = remaining_pages(pattern) × (current_cost_per_page − expected_fast_cost) × P(success)
             ─────────────────────────────────────────────────────────────────────────────────
                                 expected_agent_cost(job type, history)
```

`P(success)` and `expected_agent_cost` are estimated from past episodes of the same job type and site signature, so the Orchestrator's prioritisation also improves over time.

It enforces global and per-job budgets (tokens, wall time, network requests from the learning share), and runs a small number of agent episodes in parallel.

| Job | Level | Trigger | Goal | Default model |
|---|---|---|---|---|
| **Investigate** | L1 | the Analyzer can't produce a passing recipe; a `slow-only` pattern's backoff has expired | a verified recipe | Sonnet 5.5, escalating to Opus 5.5 |
| **Repair** | L1 | a recipe is degraded (verifier failures, 4xx, fingerprint drift) | a fixed recipe, or retirement | Sonnet 5.5 |
| **Distill** | L2 | ≥ 3 active recipes share a site signature; a new signature is seen often | a playbook | Opus 5.5 |
| **Improve** | L3 | periodic; a recurring failure class in lessons; a repeated tool sequence in traces | a skill | Opus 5.5 |
| **Reflect** | all | after every Investigate, Repair, Distill or Improve episode | lessons | Haiku 5.5 |

### 7.3 The agent

**7.3.1 Shape.** One agent runtime: Claude with tool use, in a bounded loop. It runs **episodes**, each one a single job. The Orchestrator gives it a **brief** containing:
- the job, its goal and its budget;
- the evidence summary (for Investigate: the Analyzer's failure reason, sample URLs, reference records, and the top attribution candidates);
- **retrieved memory**: lessons matching this site signature or symptom, applicable playbooks, and the index of available skills;
- the agent's **procedure** for this job type. This is a skill, so the agent can improve it itself (§7.3.4).

**7.3.2 Episode loop:**
1. orient (read the brief and memory);
2. form hypotheses ("data comes from `/api/v2/item?id=`; the 403 is due to a missing `x-csrf` header taken from the HTML");
3. test cheaply first: offline evidence before network experiments;
4. draft an artifact;
5. dry-run it with `run_recipe` or `run_gym`;
6. submit it to the judge;
7. stop on success, budget exhaustion, or a "not solvable in v1" verdict (for example a signed API).

All tool calls and reasoning are recorded as an **episode trace**.

**7.3.3 Tools.** These are deterministic functions. Any tool that touches the network checks robots.txt and draws from the learning budget.

| Group | Tools |
|---|---|
| **Evidence (offline)** | `list_samples(pattern)`, `get_capture(id, part, range)`, `search_bodies(pattern, text\|regex)` (hits with JSON paths), `diff_samples(ids)`, `get_reference_record(id)`, `url_variables(pattern)`, `page_status(id)` |
| **Experiment (network)** | `replay_request(request_id, overrides)`, `http_fetch(url, headers)`, `render(url, {block, allow, wait})` (Lightpanda, returns a new capture), `eval_in_page(url, js)` |
| **Recipe** | `run_recipe(draft, urls\|capture_ids)` (per-page equivalence report, no activation), `submit_recipe(draft, rationale)` (sends it to the Verifier) |
| **Knowledge** | `search_lessons(query)`, `search_playbooks(signature)`, `propose_playbook(detector, template, evidence)` |
| **Method** | `list_skills()`, `read_skill(id)`, `propose_skill(kind, code, tests, rationale)`, `run_gym(candidate)` (score delta on the training split only) |

Browser tools wrap the same Lightpanda CDP harness as T3. Lightpanda's own MCP tools (`goto`, `evaluate`, `interactiveElements`) may be exposed for page exploration.

**7.3.4 Memory**, which is what makes the agent self-improving:

| Store | Contents | Written by | Read by |
|---|---|---|---|
| **Recipes** (L1) | per-pattern direct paths | Analyzer, agent | Router |
| **Playbooks** (L2) | detector plus parameterised recipe template, with success stats | agent (Distill) | Analyzer (tried first on new patterns), agent briefs |
| **Skills** (L3) | **analyzer plug-ins** (for example a GraphQL persisted-query attributor or a Nuxt payload extractor); **tool macros** (a recurring tool sequence compiled into one tool); **procedures** (the agent's own job instructions) | agent (Improve) | Analyzer, agent |
| **Lessons** | structured notes: symptom → hypotheses tried → what worked → generality | agent (Reflect) | agent briefs (retrieved by signature and symptom); Improve jobs |
| **Episode traces** | full logs of every episode | runtime | Improve jobs (to find repeated work and failure classes), Orchestrator cost model |

### 7.4 The self-improvement loops

```
 L1  crawl ─► failure/no recipe ─► Investigate ─► recipe ─► Verifier ─► active ─► faster crawl
                                        │
 L2                                     └► Reflect ─► lessons ─► Distill ─► playbook ─► Gym ─► Analyzer tries it first
                                                         │
 L3                    episode traces + lessons ─────────┴► Improve ─► skill ─► Gym ─► canary ─► adopted
                                                                                            │
                     next time this class of problem appears: solved by the Analyzer, no LLM ◄┘
```

**L1 gate:** the Verifier. A candidate must pass on K held-out pages before it goes **active**. Active recipes are shadow-checked against T3 on s% of pages. The rate s decays as the recipe's verified streak grows, with a floor of about 1%. Failures demote a recipe to **degraded**, which triggers a Repair job, and after repeated failures it is **retired**. Drift signals (fingerprint change, 4xx, empty extractions) raise s immediately.

**L2 gate:** the playbook must beat a cold start on the gym's held-out sites: fewer samples needed, with equal accuracy. It then needs a live success rate above a threshold, or it is retired.

**L3 gate:**
1. The gym score improves with **no regression** in accuracy and no increase in cost.
2. A canary runs on about 5% of live patterns.
3. The skill is adopted with versioning and one-step rollback.
4. Skills that change **network behaviour** (new request primitives, interpreter changes) also need human approval. Pure analysis plug-ins and procedures do not.

**The gym:**
- **Corpus:** a frozen corpus of T3 captures with reference records. It grows automatically from real failures, new site signatures and the benchmark set, and is split into **training** and **held-out** sets by site.
- **Scoring:** it runs the whole learning plane offline (no network, budgeted LLM) and reports:
  - the share of patterns the Analyzer solves on its own (with no agent);
  - the share solved with the agent;
  - accuracy;
  - agent tokens per solved pattern;
  - samples per pattern.

**Pruning:** skills and playbooks unused or unhelpful for M gym runs are retired. Lessons are periodically merged and condensed.

### 7.5 Guardrails (the judge is out of the agent's reach)
- **Immutable to the agent:** Verifier code, the equivalence metrics and thresholds, gym scoring, the held-out split and its answers, rate limits, robots policy, the sandbox, budgets and the Orchestrator's value function.
- **Read access:** the agent sees only the training split's results.
- **Execution:** agent-written skill code runs in a sandbox (a separate process with no network or filesystem except through tool APIs) under CPU and memory limits.
- **Kill switch:** freezes all learning while the crawl plane keeps serving active artifacts.
- **Version history:** every artifact has one, with provenance (the episode id).

## 8. Measuring self-improvement
The headline curve is **total cost per crawled page (time + compute + LLM) against pages crawled**. It should keep falling while accuracy holds.

| Level | Metrics |
|---|---|
| L1 | median/p95 latency per page; success rate; tier mix (T0/T1/T2/T3 share); shadow-verified accuracy |
| L2 | slow-path samples and agent tokens needed per **new** pattern; playbook hit rate on new sites |
| L3 | **the Analyzer's autonomous solve rate** (the share of new patterns solved with no LLM); agent tokens per solved pattern; episodes per success; gym score over skill versions |

**v1 targets** (my proposal; measured and tuned toward, not release gates):
- on a warmed-up site, ≥ 70% of pages served by T0/T1;
- shadow-verified accuracy ≥ 95%;
- median latency ≥ 10× lower than T3-only;
- playbook-matched sites warm up with ≤ half the samples of a cold start;
- the Analyzer's autonomous solve rate rises across successive gym generations.

## 9. Evaluation
The full benchmark design is attached: **`docs/specs/benchmark.md`**. In summary:
- **Three tracks:**
  - synthetic sites with exact ground truth and scheduled drift;
  - a recorded real-web corpus served through a replay proxy;
  - a small live check.
- **Five protocols:**
  - cold stream;
  - cross-site transfer;
  - method generations (L3);
  - drift;
  - throughput.
- **Comparisons:** five baselines (Lightpanda-only, Chrome-only, plain fetch, algorithms-only, agent-without-memory) and six ablations.
- **Metrics:** accuracy and **silent-error rate**, cost per page, the learning curve (AULC), the break-even point, the autonomous solve rate across generations, and drift detection and repair.

**The gym** (§7.4) is the agent's internal development benchmark. It is built only from the benchmark's **train** split, so the benchmark's validation and test splits stay unseen.

## 10. Calls I made (please check)
- **Language/runtime:** TypeScript on Node, using the `puppeteer-core` → Lightpanda CDP path verified in the study. SQLite plus content-addressed body files for storage.
- **Agent:** one runtime with five job types, scheduled by a code-based Orchestrator rather than an LLM supervisor. Models: Sonnet 5.5 (Investigate/Repair), Opus 5.5 (Distill/Improve and escalation), Haiku 5.5 (Reflect).
- **LLM access:** AWS Bedrock with this machine's credentials and a configurable global spend cap. *Your question 2 is still open.*
- **robots.txt:** disallowed API paths are never called directly. *Your question 1 is still open.*
- **Recipes:** declarative, with no LLM-written code on the crawl path. LLM-written code exists only as sandboxed analyzer skills that have passed the gym.
- **Human approval** is required only for skills that change network behaviour.
- **Remaining calls:** equivalence thresholds; links (per-page links from the fast path, site-constant links collected once); `fields` verified per field; learning traffic capped at about 15% of each host's budget; scale of about 1M pages and 1k sites.

## 10a. Implementation notes (RSICrawl v0.1)
Where the prototype differs from this design, or adds to it:
- **Language and storage:** Python 3.11+ and asyncio, with JSON-file memory (`rsicrawl/memory.py`) instead of TypeScript and SQLite.
- **LLM access:** the Claude API or Amazon Bedrock (`rsicrawl/config.py`). Opus 5.5 runs every agent job except Reflect, which uses Haiku 5.5.
- **Tiers:** T2 (minimal-request render) is not implemented. Tier labels are assigned per page: T0 if the page HTML was not fetched, T1 if it was.
- **Chain-value cache** (`recipe.StepCache`): stable chain steps (tokens, build IDs, API bases) and cookie-only steps are skipped per site. Any failure clears the cache and reruns the full recipe.
- **Learning-traffic cap:** shadow renders are skipped when learning and verification traffic would exceed 15% of a host's request budget.
- **Live-rejection memory:** offline checks cannot see cookie or header requirements. Candidates that fail a live trial are excluded from re-analysis and passed to the agent as evidence.
- **Drift signals:** fast-path HTTP errors, empty extraction, an empty title, or far fewer main-text lines than usual each count as a fast-path failure.
- **Skill sandbox:** a Docker container with no network, a read-only filesystem, an unprivileged user, no capabilities, and memory/CPU/pid limits. Without Docker, the fallback is a process with rlimits and a disabled socket module (see SECURITY.md).
- **Orchestrator:** value ranking is coarse (a small admission delay for low-value jobs); a full priority queue is future work.

## 11. Left to the builder
- the clustering and DOM-diff algorithms;
- the DSL syntax;
- the database schema;
- the skill plug-in interface;
- N, K, M, the shadow-rate curve and job concurrency;
- the evidence-bundle format;
- the gym corpus format;
- how lessons are retrieved.
