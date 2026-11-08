"""test_pool.py — BrowserUpstream 排队超时单元测试。"""

from __future__ import annotations

import unittest

import browser_upstream as bu


class QueueTimeoutTests(unittest.TestCase):
    def test_lock_wait_timeout(self) -> None:
        upstream = bu.BrowserUpstream(port=9225, target_hint="unused")
        upstream._lock.acquire()
        old = bu.QUEUE_TIMEOUT
        bu.QUEUE_TIMEOUT = 0.2
        try:
            with self.assertRaises(bu.BrowserError) as ctx:
                upstream.generate({"messages": []}, timeout=1)
        finally:
            bu.QUEUE_TIMEOUT = old
            upstream._lock.release()
        self.assertIn("排队超时", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
