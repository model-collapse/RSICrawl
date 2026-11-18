"""Assemble the benchmark report (markdown) from run directories, gym scores and improve results.

python -m bench.report --config runs/report_config.json --out ../BENCHMARK_RESULTS.md
config: {"p1": {"B0": dir, ...}, "p2": {"cold": dir, "warm": dir, "train": dir}, "p3": [{"gen": 0, "val": f, "test": f,
         "train": f, "improve": f|null}, ...], "p4": dir, "final": dir}
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bench.score import score


def L(x): return "—" if x is None else x


def row(name, s):
    tm = s["tier_mix"]
    fast = round(tm.get("T0", 0) + tm.get("T1", 0), 3)
    return (f"| {name} | {s['pages']} | {s['valid_rate']:.3f} | {s['mean_f1']:.3f} | {s['silent_error_rate']:.4f} | "
            f"{fast:.2f} | {tm.get('T0', 0):.2f} / {tm.get('T1', 0):.2f} / {tm.get('T3', 0):.2f} | {s['requests_per_page']:.2f} | "
            f"{L(s['median_service_ms'])} | {L(s['warmed_median_service_ms'])} | {s['wall_s']:.0f} | {s['llm_usd']:.2f} |")


HDR = ("| System | Pages | Valid rate | Mean F1 | Silent-error rate | Fast share | Tier mix T0/T1/T3 | Requests/page | "
       "Median service ms | Warmed median ms | Wall s | LLM $ |\n|---|---|---|---|---|---|---|---|---|---|---|---|")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--config", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args(); cfg = json.load(open(a.config)); out = []
    S = {}
    def sc(d):
        if d not in S: S[d] = score(d)
        return S[d]
    out.append("# Self-Improving Crawler — Prototype Benchmark Results\n")
    out.append(cfg.get("preamble", ""))
    if "p1" in cfg:
        out.append("\n## P1 — Cold stream on the test split (C1, C2, C3)\n")
        out.append("Every system starts from empty memory. SYS keeps only its generation-0 built-in analyzer.\n")
        out.append(HDR)
        for k, d in cfg["p1"].items(): out.append(row(k, sc(d)))
    if "p2" in cfg:
        out.append("\n## P2 — Transfer: test stream after learning on the train stream (C4/L2)\n")
        out.append(HDR)
        for k, d in cfg["p2"].items(): out.append(row(k, sc(d)))
    if "p3" in cfg:
        out.append("\n## P3 — Method generations: analyzer autonomous solve rate, offline gyms (C4/L3)\n")
        out.append("| Generation | Skills | Train gym | Val gym (held-out sites) | Test gym (held-out sites) | Improve episode $ | Newly solved (train) |\n|---|---|---|---|---|---|---|")
        for g in cfg["p3"]:
            def r(f):
                if not f or not os.path.exists(f): return "—"
                x = json.load(open(f)); return f"{x['solved']}/{x['of']} ({x['rate']:.2f})"
            imp = json.load(open(g["improve"])) if g.get("improve") and os.path.exists(g["improve"]) else None
            usd = imp["spend_usd"] if imp else 0
            newly = ", ".join(sum([x.get("newly_solved", []) for x in imp["results"]], [])) if imp else ""
            out.append(f"| {g['gen']} | {g.get('skills', '')} | {r(g.get('train'))} | {r(g.get('val'))} | {r(g.get('test'))} | {usd:.2f} | {newly} |")
    for key, title in (("p4", "P4 — Drift (C5)"),):
        if key in cfg:
            s = sc(cfg[key]); out.append(f"\n## {title}\n"); out.append(HDR); out.append(row("SYS (warm) + drift", s))
            out.append("\n| Site | Drift | Applied | Wrong records after | Pages to first fallback | Pages to fast again | Fast share after |\n|---|---|---|---|---|---|---|")
            for d in s["drift_analysis"]:
                out.append(f"| {d['site']} | {d['drift']} | {d['applied']} | {d['wrong_records_after']} | {L(d['pages_to_first_fallback'])} | {L(d['pages_to_fast_again'])} | {d['fast_share_after']} |")
            out.append("\nDegradation events: " + ", ".join(f"`{x['pattern_id']}`" for x in s["degradations"]))
    # learning curves + controls + per site for the main systems
    for k in cfg.get("curves", []):
        s = sc(cfg["curves"][k]) if isinstance(cfg["curves"], dict) else None
    if cfg.get("curves"):
        out.append("\n## Learning curves (non-control patterns; page index within each pattern, all sites pooled)\n")
        names = list(cfg["curves"].keys()); curves = {n: {r["pages"]: r for r in sc(cfg["curves"][n])["learning_curve"]} for n in names}
        windows = sorted({w for c in curves.values() for w in c}, key=lambda x: int(x.split("-")[0]))
        out.append("| Pages | " + " | ".join(f"{n} req/page | {n} fast share" for n in names) + " |")
        out.append("|---|" + "---|---|" * len(names))
        for w in windows:
            out.append(f"| {w} | " + " | ".join(f"{curves[n].get(w, {}).get('requests_per_page', '—')} | {curves[n].get(w, {}).get('fast_share', '—')}" for n in names) + " |")
    if cfg.get("controls"):
        out.append("\n## Control archetypes (must be 100% compliant)\n")
        out.append("| Run | Site | Archetype | Result | Compliant |\n|---|---|---|---|---|")
        for n, d in cfg["controls"].items():
            for sid, c in sc(d)["controls"].items():
                out.append(f"| {n} | {sid} | {c['archetype']} | {json.dumps({k: v for k, v in c.items() if k not in ('archetype', 'compliant')})} | {c['compliant']} |")
    if cfg.get("per_site"):
        for n, d in cfg["per_site"].items():
            out.append(f"\n## Per-site detail — {n}\n")
            out.append("| Site | Family | Archetype | Pages | Valid rate | Fast share | Requests/page | Silent errors |\n|---|---|---|---|---|---|---|---|")
            for r in sc(d)["sites"]:
                out.append(f"| {r['site']} | {r['family']} | {r['archetype']} | {r['pages']} | {r['valid_rate']} | {r['fast_share']} | {r['requests_per_page']} | {r['silent_errors']} |")
            s = sc(d)
            out.append("\nActivated recipes: " + ", ".join(f"`{x['pattern_id'].split('/',1)[1]}`@{x['pattern_id'].split(':')[1].split('/')[0]} ← {x['origin'].split(':')[0]} ({x['tier']})" for x in s["activations"]))
            out.append(f"\nAgent episodes: {dict(s['episodes'])}; outcomes: {dict(s['episode_outcomes'])}")
    out.append("\n" + cfg.get("postamble", ""))
    open(a.out, "w").write("\n".join(out) + "\n")
    print("wrote", a.out)


if __name__ == "__main__":
    main()
