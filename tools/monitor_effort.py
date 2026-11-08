"""monitor_effort.py — 打印 CC 请求的 effort 与发往上游的 effort。"""

from __future__ import annotations

import json
import os
import time

LOG = os.getenv("BROWSER_DEBUG_LOG") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "bridge-debug.jsonl"
)


def show(e: dict) -> None:
    ts = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
    print(
        f"{ts} model={e.get('requested_model')} max_tokens={e.get('max_tokens')} "
        f"CC_effort={e.get('requested_effort')} upstream_effort={e.get('reasoning_effort')} "
        f"msgs={e.get('messages')} tools={e.get('tools')}",
        flush=True,
    )


def main() -> int:
    events = [json.loads(line) for line in open(LOG, encoding="utf-8")]
    payloads = [e for e in events if e.get("event") == "payload"]
    print("recent payloads:", flush=True)
    for e in payloads[-10:]:
        show(e)

    pos = os.path.getsize(LOG)
    start = time.time()
    print("monitoring 90s ...", flush=True)
    while time.time() - start < 90:
        time.sleep(4)
        size = os.path.getsize(LOG)
        if size <= pos:
            continue
        with open(LOG, encoding="utf-8") as f:
            f.seek(pos)
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("event") == "payload":
                    show(e)
                elif e.get("event") in ("browser_error", "empty_detected", "empty_retry_ok", "empty_retry_failed"):
                    print(
                        time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0))),
                        e.get("event"),
                        e.get("id", ""),
                        flush=True,
                    )
        pos = size
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
