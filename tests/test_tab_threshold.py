"""test_tab_threshold.py — 用独立 ktest 标签验证轮次/文本阈值刷新。"""

from __future__ import annotations

import json
import urllib.request

from browser_upstream import DEFAULT_BASE_URL, BrowserPool

BODY = {
    "model": "moonshotai/kimi-k3.1-pro",
    "max_tokens": 32,
    "messages": [{"role": "user", "content": "Say OK."}],
    "stream": True,
    "stream_options": {"include_usage": True},
}

pool = BrowserPool(port=9225, size=1, base_url=DEFAULT_BASE_URL, prefix="ktest")
try:
    for i in (1, 2):
        text = pool.generate(BODY, timeout=120)
        print(f"request {i}: sse bytes={len(text)}")
finally:
    pool.close()

# 清理测试标签
pages = [t for t in json.load(urllib.request.urlopen("http://127.0.0.1:9225/json/list")) if t.get("type") == "page"]
for page in pages:
    if "ktest=1" in (page.get("url") or ""):
        urllib.request.urlopen(f"http://127.0.0.1:9225/json/close/{page['id']}")
        print("closed", page.get("url"))
