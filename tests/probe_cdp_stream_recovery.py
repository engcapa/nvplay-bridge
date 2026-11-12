"""Opt-in real-CDP recovery checks on isolated local pages; no upstream calls.

Run with PYTHONPATH=. python3 tests/probe_cdp_stream_recovery.py --cdp-port 9225.
"""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import urllib.parse
import urllib.request
import uuid

import httpx
import websocket

import browser_upstream as browser
import nvplay_bridge as bridge
from stream_support import SSEDecoder


PAGE = b"""<!doctype html><meta charset=utf-8>
<textarea aria-label="chat prompt"></textarea><button aria-label="Send">Send</button>
<script>
window.hcaptcha = {execute() {}};
window.fetch = async () => {
  const count = Number(sessionStorage.fixtureRequests || 0) + 1;
  sessionStorage.fixtureRequests = String(count);
  const abandoned = new URL(location).searchParams.get('scenario') === 'retry' && count === 1;
  const tool = abandoned ? {name:'Bash', arguments:'{"command":"DO_NOT_RUN"}'}
                         : {name:'Read', arguments:'{"file_path":"safe.rs"}'};
  return new Response('data: ' + JSON.stringify({choices:[{delta:{tool_calls:[{
    index:0, id:'fixture-call', function:tool}]}, finish_reason:'tool_calls'}]}) + '\\n\\n',
    {status:200, headers:{'content-type':'text/event-stream'}});
};
document.querySelector('button').onclick = () => {
  fetch('/v2/predict/models/local-fixture', {method:'POST', body:'{}'})
    .then(response => response.text()).catch(() => {});
};
</script>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


class ProbeTab(browser.BrowserUpstream):
    def __init__(self, port, hint, scenario):
        super().__init__(port=port, target_hint=hint)
        self.scenario = scenario
        self.injected = False

    def _eval(self, expression, timeout=5):
        value = super()._eval(expression, timeout)
        if expression == browser.PATCH_JS and self.scenario == "delayed" and not self.injected:
            self.injected = True
            super()._eval("""(() => {
              const drain = window.__bridgeDrain;
              let first = true;
              window.__bridgeDrain = async (...args) => {
                if (first) { first = false; await new Promise(resolve => setTimeout(resolve, 6000)); }
                return drain(...args);
              };
            })()""")
        elif ("window.__bridgeDrain(" in expression and self.scenario == "retry"
              and not self.injected and value.get("data")):
            # The destructive command executed, but its reply cannot be used.
            # Closing this connection also exercises quarantine/cleanup.
            self.injected = True
            self.ws.close()
            error = websocket.WebSocketTimeoutException("fixture lost drain reply")
            error.cdp_phase, error.cdp_method = "receive", "Runtime.evaluate"
            raise error
        return value


async def check(tab, scenario):
    bridge.UPSTREAM_KIND = "browser"
    bridge._browser = tab
    bridge.BROWSER_TIMEOUT = 25
    bridge.BROWSER_STREAM_RETRIES = 1
    bridge.BROWSER_STREAM_BACKOFF = 0
    bridge.BROWSER_MESSAGES_DEFER_OUTPUT = True
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),
                                base_url="http://local-probe") as client:
        response = await client.post("/v1/messages", json={"model":"kimi-k3.1-pro", "stream":True,
            "messages":[{"role":"user", "content":"fixture"}], "max_tokens":128})
    seconds = time.monotonic() - started
    decoder = SSEDecoder()
    events = decoder.feed(response.text, final=True)
    tools = [event["content_block"] for event in events
             if event.get("type") == "content_block_start"
             and event.get("content_block", {}).get("type") == "tool_use"]
    assert response.status_code == 200 and len(tools) == 1, response.text
    assert tools[0]["name"] == "Read" and "safe.rs" in response.text, response.text
    assert "DO_NOT_RUN" not in response.text and "event: error" not in response.text, response.text
    assert any(event.get("type") == "message_stop" for event in events), response.text
    count = tab._eval("Number(sessionStorage.fixtureRequests)")
    assert count == (1 if scenario == "delayed" else 2), count
    assert scenario != "delayed" or seconds >= 6, seconds
    print(json.dumps({"scenario":scenario, "requests":count, "seconds":round(seconds, 3),
                      "tools_delivered":len(tools), "result":"pass"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-port", type=int, default=9225)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{args.cdp_port}"
    try:
        for scenario in ("delayed", "retry"):
            target = tab = None
            hint = "bridge-cdp-probe=" + uuid.uuid4().hex
            url = f"http://127.0.0.1:{server.server_port}/?{hint}&scenario={scenario}"
            try:
                request = urllib.request.Request(base + "/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")
                with browser.LOCAL_HTTP.open(request, timeout=5) as response:
                    target = json.load(response)["id"]
                tab = ProbeTab(args.cdp_port, hint, scenario)
                tab._connect()
                tab._wait_ready(threading.Event(), time.monotonic() + 10)
                asyncio.run(check(tab, scenario))
            finally:
                if tab is not None:
                    tab.close()
                if target is not None:
                    with browser.LOCAL_HTTP.open(base + "/json/close/" + target, timeout=5):
                        pass
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


if __name__ == "__main__":
    main()
