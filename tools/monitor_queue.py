"""monitor_queue.py — 监控多子 agent 下的桥队列与错误，120 秒。"""

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
            return "ok" if json.load(r).get("status") == "ok" else "bad"
    except Exception as exc:  # noqa: BLE001
        return f"DOWN {exc}"


def main() -> int:
    pos = os.path.getsize(LOG)
    starts: dict[str, dict] = {}
    dones: dict[str, dict] = {}
    errors: list[dict] = []
    placeholders: list[dict] = []
    start = time.time()
    last_report = 0
    print("start health", health(), flush=True)
    while time.time() - start < 120:
        time.sleep(4)
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
                    k = e.get("event")
                    if k == "browser_start":
                        starts[e["id"]] = e
                    elif k == "browser_done":
                        dones[e["id"]] = e
                        c = e.get("counts") or {}
                        print(
                            f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} DONE {e['id']} "
                            f"{e.get('sec')}s text={c.get('text_chars')} tools={c.get('tool_calls')} finish={e.get('finish')}",
                            flush=True,
                        )
                    elif k in ("browser_error", "model_rejected") or str(k).startswith("placeholder"):
                        (errors if k in ("browser_error", "model_rejected") else placeholders).append(e)
                        print(
                            f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} {k} "
                            f"{e.get('error') or e.get('model') or e.get('id')}",
                            flush=True,
                        )
            pos = size
        if time.time() - start - last_report >= 15:
            last_report = time.time() - start
            pending = [s for sid, s in starts.items() if sid not in dones]
            oldest = max((time.time() - s["ts"] for s in pending), default=0)
            print(
                f"QUEUE pending={len(pending)} oldest={oldest:.0f}s done={len(dones)} "
                f"errors={len(errors)} placeholders={len(placeholders)} health={health()}",
                flush=True,
            )
    print("summary", {"starts": len(starts), "done": len(dones), "errors": len(errors), "placeholders": len(placeholders)}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
