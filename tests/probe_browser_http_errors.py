"""Opt-in real-CDP error-body probe using an isolated local page, not NVIDIA.

Run with PYTHONPATH=. python3 tests/probe_browser_http_errors.py --cdp-port 9225.
The browser must already be running. Only this probe's own page is closed.
"""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import urllib.parse
import uuid

os.environ.setdefault("UPSTREAM_KIND", "browser")
import httpx

import browser_upstream as bu
import nvplay_bridge as bridge


PAGE = b"""<!doctype html><meta charset=utf-8>
<textarea aria-label="chat prompt"></textarea>
<button aria-label="Send">Send</button>
<script>
window.hcaptcha = {execute() {}};
window.fixtureRequests = 0;
window.fixtureError = "pool";
window.fetch = async function(input) {
  window.fixtureRequests++;
  const failure = window.fixtureRequests === 1;
  const enc = new TextEncoder();
  const parts = failure
    ? window.fixtureError === 'pool' ? ['data:{"detail":"Pool#acquire(Duration)',
       ' has been pending for more than the configured timeout of 45000ms"}\\n\\n'] : ['Internal', ' Server Error']
    : ['data: {"choices":[{"delta":{"content":"RECOVERED_CDP"},"finish_reason":"stop"}]}\\n\\n'];
  const stream = new ReadableStream({
    async start(controller) {
      for (const part of parts) {
        await new Promise(resolve => setTimeout(resolve, 200));
        controller.enqueue(enc.encode(part));
      }
      controller.close();
    }
  });
  return new Response(stream, {status: failure ? 500 : 200,
    headers: {'content-type': 'text/event-stream'}});
};
document.querySelector('button').onclick = () => {
  fetch('/v2/predict/models/local-fixture', {method:'POST', body:'{}'})
    .then(response => response.text()).catch(() => {});
};
</script>"""


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


async def check(browser):
    bridge.UPSTREAM_KIND = "browser"
    bridge._browser = browser
    bridge.BROWSER_TIMEOUT = 15
    bridge.BROWSER_POOL_TIMEOUT_BACKOFF = 0
    bridge.BROWSER_POOL_TIMEOUT_RETRIES = 1
    bridge.BROWSER_HTTP_5XX_RETRIES = 1
    bridge.BROWSER_HTTP_5XX_BACKOFF = 0
    bridge.BROWSER_HEARTBEAT_SEC = 0.05
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),
                                 base_url="http://local-probe") as client:
        for error_kind in ("pool", "generic"):
            for endpoint in ("messages", "responses"):
                browser._connect()
                browser._eval(f"window.fixtureRequests = 0; window.fixtureError = {json.dumps(error_kind)}")
                body = {"model": "kimi-k3.1-pro", "stream": True}
                if endpoint == "messages":
                    body["messages"] = [{"role": "user", "content": "fixture"}]
                else:
                    body.update(input="fixture", store=False)
                response = await client.post("/v1/" + endpoint, json=body)
                assert response.status_code == 200, response.text
                assert "RECOVERED_CDP" in response.text, response.text
                assert "event: error" not in response.text, response.text
                assert "response.failed" not in response.text, response.text
                assert browser._eval("window.fixtureRequests") == 2
                terminal = "message_stop" if endpoint == "messages" else "response.completed"
                assert f"event: {terminal}" in response.text, response.text
                print(json.dumps({"endpoint": endpoint, "error_kind": error_kind, "requests": 2, "result": "pass"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-port", type=int, default=9225)
    args = parser.parse_args()
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    target = None
    browser = None
    base = f"http://127.0.0.1:{args.cdp_port}"
    hint = "bridge-error-probe=" + uuid.uuid4().hex
    url = f"http://127.0.0.1:{fixture.server_port}/?{hint}"
    try:
        request = bu.urllib.request.Request(base + "/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")
        with bu.LOCAL_HTTP.open(request, timeout=5) as response:
            target = json.load(response)["id"]
        browser = bu.BrowserUpstream(port=args.cdp_port, target_hint=hint)
        browser._connect()
        browser._wait_ready(threading.Event(), bu.time.monotonic() + 10)
        asyncio.run(check(browser))
    finally:
        if browser is not None:
            browser.close()
        if target is not None:
            with bu.LOCAL_HTTP.open(base + "/json/close/" + target, timeout=5):
                pass
        fixture.shutdown()
        fixture.server_close()
        thread.join(2)


if __name__ == "__main__":
    main()
