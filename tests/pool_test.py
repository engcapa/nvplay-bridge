"""pool_test.py — 3 并发池测试 + thinking 回传 + 缺省 max_tokens 检查。"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = "http://127.0.0.1:8083"


def post(body: dict):
    with httpx.Client(timeout=httpx.Timeout(300, connect=10)) as c:
        r = c.post(BASE + "/v1/messages", json=body)
    return r


def smoke(i: int) -> dict:
    t0 = time.time()
    r = post(
        {
            "model": "kimi-k3.1-pro",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": f"Reply with exactly: P{i}"}],
        }
    )
    text = ""
    if r.status_code == 200:
        text = "".join(b.get("text", "") for b in r.json().get("content") or [] if b.get("type") == "text")
    return {"i": i, "status": r.status_code, "time": round(time.time() - t0, 1), "text": text[:30]}


print("=== 3 parallel ===")
start = time.time()
with ThreadPoolExecutor(max_workers=3) as ex:
    results = list(ex.map(smoke, [1, 2, 3]))
print("wall", round(time.time() - start, 1), "s")
for r in results:
    print(r)

print("\n=== thinking passback ===")
r = post(
    {
        "model": "kimi-k3.1-pro",
        "max_tokens": 128,
        "messages": [
            {"role": "user", "content": "What is 2+2? Answer only the number."},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "2+2 is 4."},
                {"type": "text", "text": "4"},
            ]},
            {"role": "user", "content": "Now 3+3? Answer only the number."},
        ],
    }
)
print("status", r.status_code, r.text[:400])

print("\n=== no max_tokens / no effort ===")
r = post({"model": "kimi-k3.1-pro", "messages": [{"role": "user", "content": "Say OK."}]})
print("status", r.status_code, r.text[:300])
