"""Opt-in native CDP probe: truncate a local response and capture the browser error.

Run with PYTHONPATH=. python3 tests/probe_cdp_network.py --cdp-port 9225.
Only an isolated local fixture tab is created; no NVIDIA requests are made.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import urllib.parse
import uuid

import browser_upstream as browser
from tools.monitor_cdp_network import observe

PAGE = b"""<!doctype html><meta charset=utf-8>
<textarea aria-label="chat prompt"></textarea>
<button aria-label="Send">Send</button>
<script>
window.hcaptcha={execute(){}};
document.querySelector('button').onclick=()=>{
  fetch('/v2/predict/models/truncated-local-fixture',{method:'POST',body:'{}'})
    .then(r=>r.text()).catch(()=>{});
};
</script>"""


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", "100000")
        self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"PARTIAL_LOCAL_CDP"}}]}\n\n')
        self.wfile.flush()
        time.sleep(0.5)
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-port", type=int, default=9225)
    args = parser.parse_args()
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    serving = threading.Thread(target=fixture.serve_forever, daemon=True)
    serving.start()
    prefix = "bridge-network-probe=" + uuid.uuid4().hex
    base = f"http://127.0.0.1:{args.cdp_port}"
    url = f"http://127.0.0.1:{fixture.server_port}/?{prefix}"
    stop, connected, failed = threading.Event(), threading.Event(), threading.Event()
    events = []
    target = tab = observer = None

    def emit(event):
        events.append(event)
        if event["event"] == "observer_connected":
            connected.set()
        if event["event"] == "prediction_failed":
            failed.set()

    try:
        request = browser.urllib.request.Request(base + "/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")
        with browser.LOCAL_HTTP.open(request, timeout=5) as response:
            target = json.load(response)
        observer = threading.Thread(target=observe, args=(target, stop, emit), daemon=True)
        observer.start()
        assert connected.wait(5), "network observer did not connect"
        tab = browser.BrowserUpstream(port=args.cdp_port, target_hint=prefix)
        tab._connect()
        tab._wait_ready(threading.Event(), time.monotonic() + 10)
        partial = []
        error = None
        try:
            partial.extend(tab.stream({}, timeout=10))
        except browser.BrowserError as caught:
            error = str(caught)
        assert error and "读取失败" in error, error
        assert "PARTIAL_LOCAL_CDP" in "".join(partial), partial
        assert failed.wait(5), events
        failure = next(event for event in events if event["event"] == "prediction_failed")
        assert failure["status"] == 200, failure
        assert "ERR_CONTENT_LENGTH_MISMATCH" in failure["errorText"], failure
        print(json.dumps({"result": "pass", "error": error, "transport": failure,
                          "partial_bytes_delivered": True, "nvidia_requests": 0}))
    finally:
        stop.set()
        if observer is not None:
            observer.join(3)
        if tab is not None:
            tab.close()
        if target is not None:
            with browser.LOCAL_HTTP.open(base + "/json/close/" + target["id"], timeout=5):
                pass
        fixture.shutdown()
        fixture.server_close()
        serving.join(2)


if __name__ == "__main__":
    main()
