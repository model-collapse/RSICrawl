"""Minimal async CDP client for Lightpanda: render a URL and capture DOM + all network traffic."""
import asyncio, json, os, subprocess, time, base64, itertools, socket
import websockets

from .config import lightpanda_bin
LIGHTPANDA = lightpanda_bin()


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class LightpandaServer:
    """One `lightpanda serve` process; each CDP connection gets its own browser thread."""
    def __init__(self, port=None, max_conn=32):
        self.port = port or free_port()
        env = {**os.environ, "LIGHTPANDA_DISABLE_TELEMETRY": "true"}
        self.proc = subprocess.Popen([LIGHTPANDA, "serve", "--host", "127.0.0.1", "--port", str(self.port),
                                      "--log-level", "fatal", "--cdp-max-connections", str(max_conn)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.1).close(); break
            except OSError: time.sleep(0.05)

    @property
    def ws(self): return f"ws://127.0.0.1:{self.port}"

    def close(self):
        self.proc.kill(); self.proc.wait()


class CDPSession:
    def __init__(self, ws):
        self.ws = ws; self.ids = itertools.count(1); self.pending = {}; self.handlers = []
        self.reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, url):
        ws = await websockets.connect(url, max_size=64 * 1024 * 1024)
        return cls(ws)

    async def _read(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in self.pending:
                    self.pending.pop(msg["id"]).set_result(msg)
                else:
                    for h in list(self.handlers): h(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            for f in self.pending.values():
                if not f.done(): f.set_exception(ConnectionError("cdp closed"))

    async def send(self, method, params=None, session_id=None, timeout=30):
        i = next(self.ids); fut = asyncio.get_running_loop().create_future(); self.pending[i] = fut
        msg = {"id": i, "method": method, "params": params or {}}
        if session_id: msg["sessionId"] = session_id
        await self.ws.send(json.dumps(msg))
        res = await asyncio.wait_for(fut, timeout)
        if "error" in res: raise RuntimeError(f"{method}: {res['error']}")
        return res.get("result", {})

    async def close(self):
        await self.ws.close(); self.reader.cancel()


async def render(ws_url, url, *, wait="networkidle", timeout=20.0, block=None, allow_only=None,
                 max_body=2_000_000, idle_ms=500):
    """Render `url` in a fresh browser context. Returns capture dict.

    block(url)->bool : requests for which it returns True are failed (robots, blocklists).
    allow_only(url)->bool : if given, sub-requests for which it returns False are failed (T2 minimal mode).
    """
    t0 = time.monotonic()
    cdp = await CDPSession.connect(ws_url)
    reqs, order = {}, []
    blocked = []
    state = {"load": False, "last_net": time.monotonic(), "inflight": 0}
    sid_box = {}

    def on(msg):
        m, p = msg.get("method"), msg.get("params", {})
        if m == "Network.requestWillBeSent":
            rid = p["requestId"]; r = p["request"]
            if rid not in reqs: order.append(rid)
            reqs[rid] = {"id": rid, "url": r["url"], "method": r.get("method", "GET"),
                         "headers": r.get("headers", {}), "post_data": r.get("postData"),
                         "type": p.get("type"), "status": None, "resp_headers": {}, "mime": None, "body": None}
            state["inflight"] += 1; state["last_net"] = time.monotonic()
        elif m == "Network.responseReceived":
            r = reqs.get(p["requestId"])
            if r:
                resp = p["response"]; r["status"] = resp.get("status"); r["resp_headers"] = resp.get("headers", {})
                r["mime"] = resp.get("mimeType")
        elif m in ("Network.loadingFinished", "Network.loadingFailed"):
            state["inflight"] = max(0, state["inflight"] - 1); state["last_net"] = time.monotonic()
            if m == "Network.loadingFailed" and p["requestId"] in reqs: reqs[p["requestId"]]["failed"] = True
        elif m == "Page.loadEventFired":
            state["load"] = True
        elif m == "Fetch.requestPaused":
            u = p["request"]["url"]; is_main = p.get("resourceType") == "Document" and u == url
            deny = (block and block(u)) or (allow_only and not is_main and not allow_only(u))
            if deny: blocked.append(u)
            asyncio.ensure_future(cdp.send("Fetch.failRequest" if deny else "Fetch.continueRequest",
                                           {"requestId": p["requestId"], **({"errorReason": "BlockedByClient"} if deny else {})},
                                           session_id=sid_box.get("sid")))
    cdp.handlers.append(on)
    try:
        ctx = await cdp.send("Target.createBrowserContext")
        tgt = await cdp.send("Target.createTarget", {"url": "about:blank", "browserContextId": ctx["browserContextId"]})
        att = await cdp.send("Target.attachToTarget", {"targetId": tgt["targetId"], "flatten": True})
        sid = att["sessionId"]; sid_box["sid"] = sid
        await cdp.send("Page.enable", session_id=sid)
        await cdp.send("Network.enable", session_id=sid)
        if block or allow_only:
            await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session_id=sid)
        nav = await cdp.send("Page.navigate", {"url": url}, session_id=sid, timeout=timeout)
        deadline = t0 + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            quiet = (time.monotonic() - state["last_net"]) * 1000 >= idle_ms and state["inflight"] == 0
            if state["load"] and (wait == "load" or quiet): break
        ev = await cdp.send("Runtime.evaluate", {"expression": "document.documentElement ? document.documentElement.outerHTML : ''",
                                                 "returnByValue": True}, session_id=sid)
        html = ev.get("result", {}).get("value", "")
        for rid in order:
            r = reqs[rid]
            if r.get("failed") or r["status"] is None: continue
            try:
                b = await cdp.send("Network.getResponseBody", {"requestId": rid}, session_id=sid, timeout=5)
                body = base64.b64decode(b["body"]).decode("utf-8", "replace") if b.get("base64Encoded") else b.get("body", "")
                r["body"] = body[:max_body]
            except Exception as e:
                r["body_error"] = str(e)[:100]
        status = None
        for rid in order:
            if reqs[rid]["url"].split("#")[0] == url.split("#")[0] or reqs[rid].get("type") == "Document":
                status = reqs[rid]["status"]; break
        return {"url": url, "final_html": html, "status": status, "requests": [reqs[r] for r in order],
                "blocked": blocked, "elapsed_ms": int((time.monotonic() - t0) * 1000), "loaded": state["load"]}
    finally:
        try: await cdp.close()
        except Exception: pass


if __name__ == "__main__":
    import sys
    srv = LightpandaServer()
    try:
        cap = asyncio.run(render(srv.ws, sys.argv[1]))
        print(json.dumps({k: v for k, v in cap.items() if k != "final_html"}, indent=1)[:3000])
        print("HTML len", len(cap["final_html"]), cap["final_html"][:300])
    finally:
        srv.close()
