# Changelog

## 0.1.0 (2026-11-18): first public research prototype
- Crawl plane: Lightpanda (CDP) full-render capture, fast tiers T0/T1 driven by declarative recipes, shadow checks, drift signals, degrade and re-learn.
- Learning plane: algorithmic analyzer (gen-0), Claude agent with Investigate/Repair/Distill/Improve/Reflect jobs, playbooks, sandboxed skill plug-ins, lessons, and a gym judge.
- SiteForge synthetic benchmark (7 families × 12 archetypes, drift events, optional realistic sub-resources) with runner, scorer, speed criteria and report.
- Results: see `results/BENCHMARK_RESULTS.md` (single seed, synthetic sites only).
- Added after the published benchmark runs: link templates over lists of scalar ids (e.g. `"related": [12, 13]`), docker skill sandbox, configurable LLM provider and paths. Not exercised by the published results.
