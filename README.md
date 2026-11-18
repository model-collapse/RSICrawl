<p align="center"><img src="assets/logo.svg" width="200" alt="RSICrawl logo: a round spider-robot with a magnifying glass, circled by a self-improvement arrow"></p>

<h1 align="center">RSICrawl</h1>
<p align="center"><b>A web crawler that improves itself.</b><br>
An LLM agent learns how each site actually delivers its data, then compiles what it learned into recipes, cross-site playbooks and new analyzer code, so over time it needs the browser, and itself, less and less.</p>

<p align="center">
<a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="Apache-2.0"></a>
<img src="https://img.shields.io/badge/status-research%20prototype-orange.svg" alt="research prototype">
<img src="https://img.shields.io/badge/python-3.11%2B-informational.svg" alt="python 3.11+">
</p>

> **Status: research prototype (v0.1).** Every result below comes from a synthetic benchmark of locally generated websites (single seed per configuration). It has **not** yet been evaluated on real websites. Read [`results/BENCHMARK_RESULTS.md`](results/BENCHMARK_RESULTS.md) for the full picture, including what failed.

## The idea

Pages that share a URL pattern (`shop.com/product/{id}`) share a template, and their main content comes from one source: a JSON API the page calls, JSON embedded in the HTML, or the HTML itself. Rendering every page in a browser repeats the same discovery millions of times. RSICrawl renders a few pages, works out where the data really comes from, and from then on fetches it directly.

It improves at three levels, each compounding on the one below:

| Level | What improves | Artifact | Effect |
|---|---|---|---|
| **L1 knowledge** | how to get one pattern's data directly | **recipe** (declarative JSON) | that pattern no longer needs a browser |
| **L2 strategy** | how a *kind* of site works | **playbook** | new sites of that kind are learned from 2 pages, often with no LLM |
| **L3 method** | how the crawler analyses pages | **skill** (Python plug-in written by the agent) | a whole class of problems is solved by code from then on |

The agent progressively makes itself unnecessary. Everything it produces must first pass a **judge it cannot change**: live trials and shadow checks against full renders for recipes, and an offline benchmark ("gym") with held-out sites for playbooks and skills.

```
 CRAWL PLANE (no LLM)                         LEARNING PLANE
 frontier → pattern → router ─┬ T0 direct API ─┐      analyzer (built-ins + skills + playbooks)
                              ├ T1 HTML/JSON  ─┼→ record     │ can't solve
                              └ T3 Lightpanda ─┘      orchestrator → agent (Claude, tool use)
                                 full render + capture  │  investigate · repair · distill · improve · reflect
                                         │               └→ recipes · playbooks · skills · lessons
                                         └──────── evidence ───────┘   judged by: verifier + gym
```

## Results (synthetic benchmark, SiteForge)

Fresh sites (seed 23, never used for tuning), realistic page weight, polite crawling at 2 requests/s per host, compared with rendering every page in [Lightpanda](https://github.com/lightpanda-io/browser):

| | Lightpanda-only | RSICrawl (warm) |
|---|---|---|
| Correct records | 100% | **100%**, 0 silently wrong |
| Median page latency (warmed) | 400 ms | **39 ms (10.3×)** |
| Pages per host-hour | 1× | **6.0×** |
| Pages needing a full render | 100% | 11% |
| Control sites (signed API, bot wall, robots-disallowed API) | — | all 6 handled correctly (refused or skipped) |
| LLM cost | — | $0.63 for ~1,900 pages |

Self-improvement in method (L3): on held-out test sites, the share of URL patterns the analyzer solves **without any LLM** went **75% → 92% → 100%** over two agent-written skills, which cost $0.60 and $0.40 to write. Site changes (an API version bump, build-ID rotation, field rename, a newly required CSRF header, SSR→CSR) caused **0 wrong records** and were re-learned within 1–7 pages.

What did *not* go well, and why, is documented too. For example: the first version failed both speed criteria, and an offline check turned out to be blind to cookie requirements. See [`results/BENCHMARK_RESULTS.md`](results/BENCHMARK_RESULTS.md).

## Quick start

```bash
git clone https://github.com/model-collapse/RSICrawl && cd RSICrawl
pip install -e ".[dev]"            # add ".[bedrock]" for AWS Bedrock

# Lightpanda (headless browser, separate install; Linux x86_64 shown)
curl -L -o lightpanda https://github.com/lightpanda-io/browser/releases/download/nightly/lightpanda-x86_64-linux
chmod +x lightpanda && export RSICRAWL_LIGHTPANDA=$PWD/lightpanda LIGHTPANDA_DISABLE_TELEMETRY=true

pytest -q                          # unit tests + one end-to-end crawl (no LLM needed)

# A small benchmark without an LLM: generate synthetic sites, crawl with the analyzer only (system B3)
python -m siteforge.generate --seed 5 --out runs/forge5 --items 60
python -m bench.run --forge runs/forge5 --system B3 --split test --out runs/demo_b3
python -m bench.score runs/demo_b3

# The full self-improving system (needs Claude: ANTHROPIC_API_KEY, or RSICRAWL_LLM_PROVIDER=bedrock)
python -m bench.run --forge runs/forge5 --system SYS --split test --out runs/demo_sys --spend-cap 5
```

Systems: `B0` render only, `B2` plain fetch, `B3` analyzer only, `B4` agent without memory, `SYS` full system. Useful flags: `--memory-from DIR` (start from learned playbooks, skills and lessons), `--drift` (unannounced site changes), `--subres 6` (realistic page weight), `--rate`, `--spend-cap`. Use `scripts/reproduce.sh` to rerun the whole published benchmark.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RSICRAWL_LIGHTPANDA` | `lightpanda` on PATH | Lightpanda binary |
| `RSICRAWL_LLM_PROVIDER` | `anthropic` if `ANTHROPIC_API_KEY` is set, else `bedrock` | Claude API or Amazon Bedrock |
| `RSICRAWL_MODEL_AGENT` / `RSICRAWL_MODEL_REFLECT` | `claude-opus-5-5` / `claude-haiku-5-5` | agent and lesson-writing models |
| `RSICRAWL_AWS_REGION`, `RSICRAWL_BEDROCK_PREFIX` | `us-west-2`, `us.anthropic.` | Bedrock settings |
| `RSICRAWL_SANDBOX` | `docker` if available, else `process` | isolation for agent-written skills (see [SECURITY.md](SECURITY.md)) |
| `RSICRAWL_USER_AGENT` | `RSICrawl/0.1 (+repo URL)` | please add your own contact details |

## Repository layout

| Path | Contents |
|---|---|
| `rsicrawl/` | the crawler: CDP capture, content isolation, recipe DSL and interpreter, analyzer, agent, engine, memory |
| `siteforge/` | synthetic benchmark websites with ground truth, archetypes, drift control |
| `bench/` | protocol runner, scorer, speed criteria, gym builder and scorer, L3 generation step, report |
| `docs/specs/` | the [design](docs/specs/design.md) and [benchmark](docs/specs/benchmark.md) specifications |
| `results/` | the benchmark report, per-run summaries, gym scores, and the learned skills and playbooks |
| `tests/` | unit tests and an end-to-end test |

## Limitations

- **Synthetic sites only, one seed.** The real-web tracks of the benchmark (recorded replay, live week) have not been run. Generated sites share one HTML skeleton, and some distilled playbooks fit it too closely.
- **Not production software.** It runs on one machine and stores its memory as JSON files. The orchestrator's prioritisation is coarse, and the minimal-render tier (T2) is not implemented.
- **Agent-written code runs on your machine** (sandboxed; read [SECURITY.md](SECURITY.md)).
- **Crawl responsibly**: robots.txt and rate limits are enforced in code, but you are responsible for how you use it ([RESPONSIBLE_USE.md](RESPONSIBLE_USE.md)).

## Acknowledgements

RSICrawl uses the [Lightpanda](https://github.com/lightpanda-io/browser) headless browser (AGPL-3.0, installed separately and driven over CDP), and [Claude](https://www.anthropic.com/claude) as the crawl-engineering agent. [`docs/lightpanda-study.md`](docs/lightpanda-study.md) is the study of Lightpanda that motivated the project.

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for third-party notes.
