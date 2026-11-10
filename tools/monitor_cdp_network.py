"""Observe prediction-request transport metadata on existing pool tabs via CDP."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import urllib.request

import websocket

LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PredictionRequests:
    """Keep only bounded metadata; never retain request bodies, headers or tokens."""

    def __init__(self, maximum=64):
        self.requests = {}
        self.maximum = maximum

    def feed(self, event):
        method, params = event.get("method"), event.get("params", {})
        request_id = params.get("requestId")
        if method == "Network.requestWillBeSent":
            request = params.get("request", {})
            if "/v2/predict/models/" not in request.get("url", ""):
                return None
            if len(self.requests) >= self.maximum:
                self.requests.pop(next(iter(self.requests)))
            self.requests[request_id] = {"cdp_request_id": request_id,
                                         "started_monotonic": params.get("timestamp")}
            return {"event": "prediction_request", "cdp_request_id": request_id,
                    "method": request.get("method")}
        request = self.requests.get(request_id)
        if request is None:
            return None
        if method == "Network.responseReceived":
            response = params.get("response", {})
            request.update({key: response[key] for key in ("status", "protocol", "mimeType") if key in response})
            return {"event": "prediction_response", "cdp_request_id": request_id,
                    **{key: request[key] for key in ("status", "protocol", "mimeType") if key in request}}
        if method not in ("Network.loadingFailed", "Network.loadingFinished"):
            return None
        request = dict(self.requests.pop(request_id))
        started = request.pop("started_monotonic")
        if isinstance(started, (int, float)) and isinstance(params.get("timestamp"), (int, float)):
            request["elapsed_seconds"] = round(max(0, params["timestamp"] - started), 3)
        request["event"] = "prediction_failed" if method == "Network.loadingFailed" else "prediction_finished"
        if method == "Network.loadingFailed":
            for key in ("errorText", "canceled", "blockedReason"):
                if key in params:
                    value = params[key]
                    request[key] = value[:512] if isinstance(value, str) else value
        elif "encodedDataLength" in params:
            request["encoded_bytes"] = params["encodedDataLength"]
        return request


def observe(target, stop, emit):
    while not stop.is_set():
        connection = None
        try:
            connection = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=1)
            connection.send(json.dumps({"id": 1, "method": "Network.enable", "params": {"maxPostDataSize": 0}}))
            requests = PredictionRequests()
            emit({"event": "observer_connected", "target_id": target["id"]})
            while not stop.is_set():
                try:
                    raw = connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise ConnectionError("CDP observer connection closed")
                event = requests.feed(json.loads(raw))
                if event:
                    emit({**event, "target_id": target["id"]})
        except (OSError, ValueError, websocket.WebSocketException) as error:
            emit({"event": "observer_error", "target_id": target["id"], "error": str(error)[:512]})
            stop.wait(2)
        finally:
            if connection is not None:
                connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-port", type=int, default=9225)
    parser.add_argument("--prefix", default="kpool")
    parser.add_argument("--duration", type=float, default=0, help="Zero runs until interrupted")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.duration < 0:
        parser.error("duration must be nonnegative")
    stop, lock = threading.Event(), threading.Lock()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def emit(event):
        with lock, args.output.open("a") as output:
            output.write(json.dumps({"ts": time.time(), **event}, ensure_ascii=False) + "\n")

    with LOCAL_HTTP.open(f"http://127.0.0.1:{args.cdp_port}/json/list", timeout=5) as response:
        targets = [target for target in json.load(response)
                   if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
                   and f"{args.prefix}=" in target.get("url", "")]
    if not targets:
        parser.error("no existing pool tabs match the selected prefix")
    print(json.dumps({"observing_tabs": len(targets), "output": str(args.output)}), flush=True)
    try:
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            for target in targets:
                pool.submit(observe, target, stop, emit)
            try:
                stop.wait(args.duration or None)
            finally:
                stop.set()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
