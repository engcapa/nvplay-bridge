"""monitor_bridge.py — 实时监控 bridge debug 日志 90 秒。"""

from __future__ import annotations

import json
import os
import time
import urllib.request

LOG = os.getenv("BROWSER_DEBUG_LOG") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "bridge-debug.jsonl"
)
HEALTH = "http://127.0.0.1:8083/health"


def health() -> str:
    try:
        with urllib.request.urlopen(HEALTH, timeout=3) as r:
            data = json.load(r)
        return f"ok model={data.get('model')}"
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
    pos = os.path.getsize(LOG)
    print("start size", pos, "health", health(), flush=True)
    start = time.time()
    counts: dict[str, int] = {}
    last_health = 0
    while time.time() - start < 90:
        time.sleep(5)
        size = os.path.getsize(LOG)
        if size > pos:
            with open(LOG, encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = e.get("event", "?")
                    counts[kind] = counts.get(kind, 0) + 1
                    print(fmt(e), flush=True)
            pos = size
        if time.time() - start - last_health > 15:
            last_health = time.time() - start
            print("health:", health(), flush=True)
    print("summary:", counts, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
