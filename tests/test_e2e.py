"""End-to-end: generated sites -> Lightpanda -> analyzer-only crawl (no LLM). Needs the Lightpanda binary."""
import json, os, shutil, subprocess, sys, pytest
from rsicrawl.config import lightpanda_bin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.e2e


@pytest.mark.skipif(not (shutil.which(lightpanda_bin()) or os.path.exists(lightpanda_bin())), reason="lightpanda not installed")
def test_analyzer_only_crawl(tmp_path):
    forge, out = tmp_path / "forge", tmp_path / "run"
    subprocess.run([sys.executable, "-m", "siteforge.generate", "--seed", "5", "--out", str(forge), "--items", "30"], cwd=ROOT, check=True)
    sites = [s["site_id"] for s in json.load(open(forge / "sites.json")) if s["family"] == "next" and s["archetype"] in ("A2", "A3")][:2]
    subprocess.run([sys.executable, "-m", "bench.run", "--forge", str(forge), "--system", "B3", "--split", "train,val,test",
                    "--only", ",".join(sites), "--rate", "20", "--out", str(out)], cwd=ROOT, check=True, timeout=600)
    from bench.score import score
    s = score(str(out), str(forge))
    assert s["valid_rate"] == 1.0 and s["silent_error_rate"] == 0.0
    assert s["tier_mix"].get("T0", 0) + s["tier_mix"].get("T1", 0) > 0.3
