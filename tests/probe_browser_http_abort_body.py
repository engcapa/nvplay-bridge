"""Real-CDP probe for a frontend abort immediately after HTTP error headers.

Uses only its own local HTTP page and tab; it never sends NVIDIA requests.
Run with PYTHONPATH=. python3 tests/probe_browser_http_abort_body.py --cdp-port 9225.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import urllib.parse
import uuid

import browser_upstream as bu


PAGE = b"""<!doctype html><meta charset=utf-8>
<textarea aria-label="chat prompt"></textarea><button aria-label="Send">Send</button>
<script>
window.hcaptcha = {execute() {}};
document.querySelector('button').onclick = () => {
  const signal = new AbortController();
  fetch('/v2/predict/models/local-fixture' + location.search,
    {method:'POST', body:'{}', signal:signal.signal}).then(response => {
      signal.abort();
      return response.text();
    }).catch(() => {});
};
</script>"""


class FixtureHandler(BaseHTTPRequestHandler):
    headers_sent = threading.Event()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        status = int(query["status"][0])
        delay = float(query["delay"][0])
        payload = json.dumps({"detail": f"FIXTURE_HTTP_{status}_DETAIL"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.flush()
        self.headers_sent.set()
        time.sleep(delay)
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def check(port, server_port, status, *, cancel_after_headers=False, expect_missing=False):
    target = None
    browser = None
    cancel = threading.Event()
    cancelled_at = []
    FixtureHandler.headers_sent.clear()
    base = f"http://127.0.0.1:{port}"
    hint = "bridge-abort-body-probe=" + uuid.uuid4().hex
    url = (f"http://127.0.0.1:{server_port}/?{hint}&status={status}"
           f"&delay={2 if cancel_after_headers else 0.2}")
    timer = None
    try:
        request = bu.urllib.request.Request(base + "/json/new?" + urllib.parse.quote(url, safe=""), method="PUT")
        with bu.LOCAL_HTTP.open(request, timeout=5) as response:
            target = json.load(response)["id"]
        browser = bu.BrowserUpstream(port=port, target_hint=hint)
        browser._connect()
        browser._wait_ready(threading.Event(), time.monotonic() + 10)
        if cancel_after_headers:
            def cancel_on_headers():
                if FixtureHandler.headers_sent.wait(10):
                    time.sleep(0.05)
                    cancelled_at.append(time.monotonic())
                    cancel.set()
            timer = threading.Thread(target=cancel_on_headers, daemon=True)
            timer.start()
        started = time.monotonic()
        try:
            list(browser.stream({}, timeout=5, cancel=cancel))
        except bu.BrowserCancelled:
            assert cancel_after_headers
            assert cancelled_at and time.monotonic() - cancelled_at[0] < 1.5
            result = "client_cancel_preserved"
        except bu.BrowserHTTPError as error:
            assert not cancel_after_headers
            assert error.status == status
            present = f"FIXTURE_HTTP_{status}_DETAIL" in str(error)
            assert present != expect_missing, str(error)
            result = "baseline_missing_detail_reproduced" if expect_missing else "error_detail_preserved"
        except bu.BrowserError:
            assert status == 200 and not cancel_after_headers
            result = "success_stream_frontend_abort_preserved"
        else:
            raise AssertionError("frontend abort must not produce a successful response")
        assert not browser._lock.locked()
        return {"status": status, "cancel_after_headers": cancel_after_headers,
                "result": result, "seconds": round(time.monotonic() - started, 3)}
    finally:
        if browser is not None:
            browser.close()
        if target is not None:
            with bu.LOCAL_HTTP.open(base + "/json/close/" + target, timeout=5):
                pass
        if timer is not None:
            timer.join(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-port", type=int, default=9225)
    parser.add_argument("--expect-missing-detail", action="store_true", help="Record the unfixed diagnostic failure")
    args = parser.parse_args()
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    try:
        cases = [(400, False), (500, False)] if args.expect_missing_detail else [
            (400, False), (500, False), (200, False), (400, True)]
        for status, cancelled in cases:
            print(json.dumps(check(args.cdp_port, fixture.server_port, status,
                                   cancel_after_headers=cancelled, expect_missing=args.expect_missing_detail)), flush=True)
    finally:
        fixture.shutdown()
        fixture.server_close()
        thread.join(2)


if __name__ == "__main__":
    main()
