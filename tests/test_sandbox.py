import os, shutil, pytest
from conftest import samples
from rsicrawl import analyzer as an

EVIL = """import socket
def generate(ev):
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2); return [{"steps": [], "output": {}, "net": "open"}]
    except Exception:
        return []
"""


@pytest.mark.parametrize("mode", ["process", "docker"])
def test_skill_cannot_use_network(tmp_path, monkeypatch, mode):
    if mode == "docker" and not shutil.which("docker"): pytest.skip("docker not available")
    monkeypatch.setenv("RSICRAWL_SANDBOX", mode)
    p = tmp_path / "evil.py"; p.write_text(EVIL)
    s, _ = samples("xhr")
    recipes, err = an.run_skill_sandboxed(str(p), s[:1])
    assert recipes == []
