"""Follow bridge metadata, or print a snapshot of request activity."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import time
import urllib.request

LOG = os.getenv("BROWSER_DEBUG_LOG") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "bridge-debug.jsonl"
)
HEALTH = "http://127.0.0.1:8083/health"
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LogTail:
    """Track bytes and keep unfinished UTF-8/JSON lines until the next read."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.pending = b""
        self.identity = None

    def read(self):
        try:
            with self.path.open("rb") as source:
                stat = os.fstat(source.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if identity != self.identity or stat.st_size < self.offset:
                    self.offset = 0
                    self.pending = b""
                self.identity = identity
                source.seek(self.offset)
                data = source.read()
                self.offset = source.tell()
        except FileNotFoundError:
            return []
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()
        records = []
        for line in lines:
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except (ValueError, UnicodeDecodeError):
                continue
        return records


def snapshot(records, now=None, since=None):
    """Replay request lifetimes, applying the window only to event/error totals."""
    now = time.time() if now is None else now
    active = {}
    counts = Counter()
    errors = []
    upstream_errors = []
    retries = []
    client_errors = []
    tool_errors = []
    for record in records:
        kind = record.get("event", "?")
        recent = since is None or record.get("ts", 0) >= since
        if recent:
            counts[kind] += 1
        request_id = record.get("id")
        if kind == "browser_start":
            previous = active.get(request_id, {})
            active[request_id] = {"id": request_id, "started": previous.get("started", record["ts"]),
                                  "retry_count": previous.get("retry_count", 0)}
            for key in ("reasoning_effort", "last_retry"):
                if key in previous:
                    active[request_id][key] = previous[key]
        if request_id in active:
            request = active[request_id]
            request.update(phase=kind, last_activity=record["ts"])
            for key in ("attempt", "tab_id", "counts", "usage", "reasoning_effort"):
                if key in record:
                    request[key] = record[key]
            if kind == "upstream_retry":
                request["retry_count"] += 1
                request["last_retry"] = {key: record[key] for key in ("reason", "status", "delay") if key in record}
        if kind in ("browser_done", "browser_error", "browser_cancelled"):
            active.pop(request_id, None)
        if recent and kind in ("browser_error", "pool_recovery_error"):
            errors.append(record)
        if recent and kind in ("messages_failed", "responses_failed"):
            client_errors.append(record)
        if recent and ((kind == "tool_result" and record.get("is_error"))
                       or (kind == "tool_call" and record.get("anomaly"))):
            tool_errors.append(record)
        if recent and kind in ("browser_http_error", "upstream_retry"):
            tagged = dict(record)
            if request_id in active and "reasoning_effort" in active[request_id]:
                tagged["reasoning_effort"] = active[request_id]["reasoning_effort"]
            (upstream_errors if kind == "browser_http_error" else retries).append(tagged)
    for request in active.values():
        request["age_seconds"] = round(now - request.pop("started"), 1)
        request["activity_age_seconds"] = round(now - request.pop("last_activity"), 1)
    return {"events": dict(counts), "active": list(active.values()), "recent_errors": errors[-10:],
            "recent_upstream_errors": upstream_errors[-10:], "recent_retries": retries[-10:],
            "recent_client_errors": client_errors[-10:], "recent_tool_errors": tool_errors[-10:]}


def health(url=HEALTH) -> str:
    try:
        with LOCAL_HTTP.open(url, timeout=3) as r:
            data = json.load(r)
        tabs = Counter((data.get("browser_pool") or {}).get("tabs", {}).values())
        return f"{data.get('status')} model={data.get('model')} tabs={dict(tabs)}"
    except Exception as exc:  # noqa: BLE001
        return f"DOWN {exc}"


def fmt(e: dict) -> str:
    kind = e.get("event")
    ts = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
    if kind == "request":
        return f"{ts} REQ  model={e.get('requested_model')} effort={e.get('explicit_effort')} msgs={e.get('messages')}"
    if kind == "browser_start":
        return f"{ts} START id={e.get('id')} msgs={e.get('messages')} tools={e.get('tools')} effort={e.get('reasoning_effort')}"
    if kind == "browser_done":
        c = e.get("counts") or {}
        return (
            f"{ts} DONE id={e.get('id')} {e.get('sec')}s bytes={e.get('bytes')} "
            f"think={c.get('thinking_chars')} text={c.get('text_chars')} tools={c.get('tool_calls')} finish={e.get('finish')}"
        )
    if kind == "browser_error":
        return f"{ts} ERROR id={e.get('id')} {e.get('error')}"
    if kind == "model_rejected":
        return f"{ts} REJECT model={e.get('model')}"
    return f"{ts} {kind} {json.dumps(e, ensure_ascii=False)[:200]}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=LOG)
    parser.add_argument("--health-url", default=HEALTH)
    parser.add_argument("--duration", type=float, default=90, help="Seconds; 0 follows until interrupted")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--lookback", type=float, default=3600, help="Event/error window in seconds; active requests retain their full lifetime")
    parser.add_argument("--snapshot", action="store_true")
    args = parser.parse_args()
    if args.duration < 0 or args.interval <= 0 or args.lookback < 0:
        parser.error("duration/lookback must be nonnegative and interval must be positive")
    tail = LogTail(args.log)
    records = tail.read()
    if args.snapshot:
        now = time.time()
        print(json.dumps({"health": health(args.health_url),
                          **snapshot(records, now=now, since=now - args.lookback)}, ensure_ascii=False))
        return 0
    print("start size", tail.offset, "health", health(args.health_url), flush=True)
    start = time.monotonic()
    counts: dict[str, int] = {}
    last_health = 0
    while args.duration == 0 or time.monotonic() - start < args.duration:
        remaining = max(0, args.duration - (time.monotonic() - start)) if args.duration else args.interval
        time.sleep(min(args.interval, remaining))
        for e in tail.read():
            kind = e.get("event", "?")
            counts[kind] = counts.get(kind, 0) + 1
            print(fmt(e), flush=True)
        if time.monotonic() - start - last_health > 15:
            last_health = time.monotonic() - start
            print("health:", health(args.health_url), flush=True)
    print("summary:", counts, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
