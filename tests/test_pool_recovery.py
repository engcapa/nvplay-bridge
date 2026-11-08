"""Offline recovery, readiness and HTTP streaming regression tests (no live CDP)."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

import browser_upstream as bu

os.environ.setdefault("UPSTREAM_KIND", "browser")
import nvplay_bridge as bridge


READY = {"ready": "complete", "ta": True, "editable": True, "send": True,
         "captcha_ready": True, "hc": "object", "body_chars": 34,
         "visibility": "hidden", "focus": False, "request_active": False}
WIRE = 'data: {"choices":[{"delta":{"content":"RECOVERED"},"finish_reason":"stop"}]}\n\n'


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("expected pool state transition")
        time.sleep(0.005)


class MemoryTab(bu.BrowserUpstream):
    def __init__(self, hint):
        super().__init__(target_hint=hint)
        self.calls = self.sends = self.preparation_failures = 0
        self.failure_after_send = False
        self.failure_after_data = False
        self.gate = None

    def stream(self, body, timeout=3600, cancel=None, trace=None):
        self.calls += 1
        if self.preparation_failures:
            self.preparation_failures -= 1
            raise bu.BrowserPreparationError("captcha not initialized")
        self.sends += 1
        if self.failure_after_send:
            raise bu.BrowserError("send triggered, no first byte yet")
        if self.gate is not None:
            while not self.gate.wait(0.005):
                bu._check(cancel, time.monotonic() + timeout)
        yield WIRE
        if self.failure_after_data:
            raise bu.BrowserError("reader failed")

    def page_text_len(self):
        return 34


class MemoryPool(bu.BrowserPool):
    def __init__(self, size=2, initial_ready=()):
        super().__init__(size=size)
        self.tabs = {f"kpool={n}": MemoryTab(f"kpool={n}") for n in range(1, size + 1)}
        self.initial_ready = set(initial_ready)
        self.gates = {}
        self.recoveries = []
        self.failures = {}

    def _list_pages(self):
        return [{"url": f"https://example.test/?{hint}", "webSocketDebuggerUrl": hint}
                for hint in self.tabs]

    def _inspect_page(self, ws_url):
        return dict(READY, captcha_ready=ws_url in self.initial_ready)

    def _refresh_tab(self, upstream, cancel, deadline):
        self.recoveries.append(upstream.hint)
        gate = self.gates.get(upstream.hint)
        while gate is not None and not gate.wait(0.005):
            bu._check(cancel, deadline)
        if self.failures.get(upstream.hint, 0):
            self.failures[upstream.hint] -= 1
            raise bu.BrowserError("unready page")

    def start(self):
        with patch.object(bu, "BrowserUpstream", side_effect=lambda **kw: self.tabs[kw["target_hint"]]):
            self._ensure(threading.Event(), time.monotonic() + 1)


def seeded_pool(*tabs):
    pool = bu.BrowserPool(size=len(tabs))
    pool._ready = True
    pool._ups = list(tabs)
    for tab in tabs:
        pool._states[tab.hint] = "ready"
        pool._queue.put(tab)
    return pool


class PoolRecoveryTests(unittest.TestCase):
    def test_partial_startup_serves_healthy_tab_while_another_recovers(self):
        pool = MemoryPool(initial_ready={"kpool=2"})
        pool.gates["kpool=1"] = threading.Event()
        try:
            pool.start()
            wait_for(lambda: pool.status()["tabs"].get("kpool=1") == "recovering")
            self.assertEqual(pool.generate({}, timeout=0.5), WIRE)
            self.assertEqual(pool.tabs["kpool=1"].sends, 0)
            self.assertEqual(pool.tabs["kpool=2"].sends, 1)
        finally:
            pool.close()
        self.assertFalse(pool._worker.is_alive())

    def test_failed_tab_is_quarantined_and_does_not_block_other_warmups(self):
        pool = MemoryPool(size=3)
        pool.failures["kpool=1"] = 100
        try:
            with patch.multiple(bu, TAB_RECOVERY_ATTEMPTS=1, TAB_RECOVERY_BACKOFF=60):
                pool.start()
                wait_for(lambda: pool.status()["tabs"].get("kpool=3") == "ready")
                self.assertEqual(pool.status()["tabs"]["kpool=1"], "failed")
                self.assertIn("unready page", pool.status()["errors"]["kpool=1"])
                self.assertEqual(pool.recoveries, ["kpool=1", "kpool=2", "kpool=3"])
                self.assertEqual(pool.generate({}, timeout=0.5), WIRE)
                self.assertEqual(pool.tabs["kpool=1"].sends, 0)
        finally:
            pool.close()

    def test_bounded_recovery_retries_restore_a_tab(self):
        pool = MemoryPool(size=1)
        pool.failures["kpool=1"] = 1
        try:
            pool.start()
            self.assertEqual(pool.generate({}, timeout=1), WIRE)
            self.assertEqual(pool.recoveries, ["kpool=1", "kpool=1"])
            self.assertEqual(pool.status()["errors"], {})
        finally:
            pool.close()

    def test_failed_tab_recovers_after_backoff_without_new_request(self):
        pool = MemoryPool(size=1)
        pool.failures["kpool=1"] = 1
        try:
            with patch.multiple(bu, TAB_RECOVERY_ATTEMPTS=1, TAB_RECOVERY_BACKOFF=0.01):
                pool.start()
                wait_for(lambda: pool.status()["tabs"].get("kpool=1") == "ready")
                self.assertEqual(pool.recoveries, ["kpool=1", "kpool=1"])
        finally:
            pool.close()

    def test_preparation_failure_switches_tabs_without_sending_twice(self):
        first, second = MemoryTab("a"), MemoryTab("b")
        first.preparation_failures = 1
        pool = seeded_pool(first, second)
        events = []
        try:
            self.assertEqual("".join(pool.stream({}, timeout=0.5, trace=lambda e, **d: events.append(e))), WIRE)
            self.assertEqual((first.sends, second.sends), (0, 1))
            self.assertIn("browser_prepare_retry", events)
            self.assertEqual(pool.status()["tabs"]["a"], "pending")
            self.assertEqual(pool._queue.qsize(), 1)
        finally:
            pool.close()

    def test_errors_after_send_or_data_never_switch_tabs(self):
        for after_data in (False, True):
            with self.subTest(after_data=after_data):
                first, second = MemoryTab("a"), MemoryTab("b")
                first.failure_after_send = not after_data
                first.failure_after_data = after_data
                pool = seeded_pool(first, second)
                try:
                    with self.assertRaises(bu.BrowserError):
                        list(pool.stream({}, timeout=0.5))
                    self.assertEqual((first.sends, second.sends), (1, 0))
                    self.assertEqual(pool.status()["tabs"]["a"], "pending")
                finally:
                    pool.close()

    def test_busy_tab_is_not_recovered_or_reused_by_concurrent_requests(self):
        pool = MemoryPool(initial_ready={"kpool=1", "kpool=2"})
        pool.tabs["kpool=1"].gate = threading.Event()
        results = []
        errors = []

        def generate():
            try:
                results.append(pool.generate({}, timeout=1))
            except Exception as exc:
                errors.append(exc)

        try:
            pool.start()
            thread = threading.Thread(target=generate)
            thread.start()
            wait_for(lambda: pool.status()["tabs"].get("kpool=1") == "busy")
            self.assertEqual(pool.generate({}, timeout=0.5), WIRE)
            self.assertEqual(pool.recoveries, [])
            self.assertEqual(pool.tabs["kpool=1"].sends, 1)
            pool.tabs["kpool=1"].gate.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [WIRE])
        finally:
            pool.tabs["kpool=1"].gate.set()
            pool.close()

    def test_five_concurrent_requests_keep_distinct_tabs_and_responses(self):
        release = threading.Event()

        class TaggedTab(MemoryTab):
            def stream(self, body, timeout=3600, cancel=None, trace=None):
                self.sends += 1
                while not release.wait(0.005):
                    bu._check(cancel, float("inf"))
                yield body["marker"]

        pool = MemoryPool(size=5, initial_ready={f"kpool={n}" for n in range(1, 6)})
        pool.tabs = {hint: TaggedTab(hint) for hint in pool.tabs}
        results, errors = {}, []

        def generate(index):
            try:
                results[index] = pool.generate({"marker": f"response-{index}"}, timeout=2)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=generate, args=(n,)) for n in range(5)]
        try:
            pool.start()
            for thread in threads:
                thread.start()
            wait_for(lambda: list(pool.status()["tabs"].values()).count("busy") == 5)
            self.assertEqual([tab.sends for tab in pool.tabs.values()], [1] * 5)
            self.assertEqual(pool.recoveries, [])
            release.set()
            for thread in threads:
                thread.join(1)
            self.assertEqual(errors, [])
            self.assertEqual(results, {n: f"response-{n}" for n in range(5)})
        finally:
            release.set()
            pool.close()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(1)

    def test_waiter_cancellation_leaves_background_recovery_owned_by_pool(self):
        pool = MemoryPool(size=1)
        pool.gates["kpool=1"] = threading.Event()
        cancel = threading.Event()
        try:
            pool.start()
            cancel.set()
            with self.assertRaises(bu.BrowserCancelled):
                list(pool.stream({}, timeout=0.5, cancel=cancel))
            self.assertEqual(pool.tabs["kpool=1"].sends, 0)
            pool.gates["kpool=1"].set()
            wait_for(lambda: pool.status()["tabs"].get("kpool=1") == "ready")
            self.assertEqual(pool.generate({}, timeout=0.5), WIRE)
        finally:
            pool.close()

    def test_wait_timeout_reports_pool_states(self):
        pool = seeded_pool()
        pool._states["a"] = "failed"
        pool._errors["a"] = "captcha unavailable"
        try:
            with patch.object(bu, "QUEUE_TIMEOUT", 0.01):
                with self.assertRaisesRegex(bu.BrowserError, "captcha unavailable"):
                    pool.generate({}, timeout=0.5)
        finally:
            pool.close()

    def test_missing_tab_is_recreated_before_becoming_ready(self):
        pool = bu.BrowserPool(size=1)
        tab = MemoryTab("kpool=1")
        with patch.object(pool, "_list_pages", return_value=[]), \
             patch.object(pool, "_new_blank", return_value={"webSocketDebuggerUrl": "new-page"}), \
             patch.object(pool, "_navigate") as navigate, \
             patch.object(pool, "_activate_wait"), \
             patch.object(tab, "_prepare_for_send") as prepare:
            pool._refresh_tab(tab, threading.Event(), time.monotonic() + 1)
        navigate.assert_called_once_with("new-page", pool.base_url + "?kpool=1")
        prepare.assert_called_once()


class ActivationSocket:
    def __init__(self, states):
        self.states = iter(states)
        self.commands = []
        self.closed = False

    def send(self, text):
        self.command = json.loads(text)
        self.commands.append(self.command)

    def recv(self):
        result = {}
        if (self.command["method"] == "Runtime.evaluate"
                and self.command["params"]["expression"] == bu.PAGE_STATE_JS):
            result = {"result": {"value": next(self.states)}}
        return json.dumps({"id": self.command["id"], "result": result})

    def settimeout(self, timeout):
        pass

    def close(self):
        self.closed = True


class ReadinessTests(unittest.TestCase):
    def test_small_background_page_is_ready_when_controls_and_captcha_are_ready(self):
        self.assertTrue(bu._page_ready(READY))
        self.assertFalse(bu._page_ready(dict(READY, editable=False)))
        self.assertFalse(bu._page_ready(dict(READY, captcha_ready=False)))

    def test_stalled_page_is_reactivated_and_its_lifecycle_is_resumed(self):
        ws = ActivationSocket([dict(READY, captcha_ready=False), READY])
        pool = bu.BrowserPool()
        with patch.object(bu.websocket, "create_connection", return_value=ws), \
             patch.object(bu, "TAB_REACTIVATE_INTERVAL", 0):
            pool._activate_wait("page", threading.Event(), time.monotonic() + 1)
        methods = [command["method"] for command in ws.commands]
        self.assertEqual(methods.count("Page.bringToFront"), 2)
        self.assertIn("Page.setWebLifecycleState", methods)
        self.assertIn("Emulation.setFocusEmulationEnabled", methods)
        expressions = [command["params"].get("expression") for command in ws.commands]
        self.assertIn(bu.DISMISS_JS, expressions)
        self.assertNotIn(bu.TRIGGER_JS, expressions)
        self.assertTrue(ws.closed)

    def test_readiness_timeout_preserves_diagnostics(self):
        ws = ActivationSocket([dict(READY, captcha_ready=False)])
        pool = bu.BrowserPool()
        now = [100.0]
        cancel = threading.Event()

        def advance(seconds):
            now[0] += seconds
            return False

        with patch.object(bu.websocket, "create_connection", return_value=ws), \
             patch.object(bu.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(cancel, "wait", side_effect=advance):
            with self.assertRaisesRegex(bu.BrowserError, '"visibility": "hidden"'):
                pool._activate_wait("page", cancel, now[0] + 0.02)
        self.assertTrue(ws.closed)

    def test_recovery_waits_for_new_document_instead_of_accepting_old_ready_page(self):
        class NavigatingSocket(ActivationSocket):
            def __init__(self):
                super().__init__([READY])
                self.frames = iter(("old-document", "new-document"))

            def recv(self):
                if self.command["method"] == "Page.getFrameTree":
                    return json.dumps({"id": self.command["id"], "result": {
                        "frameTree": {"frame": {"loaderId": next(self.frames)}}}})
                return super().recv()

        ws = NavigatingSocket()
        with patch.object(bu.websocket, "create_connection", return_value=ws):
            bu.BrowserPool()._activate_wait("page", threading.Event(), time.monotonic() + 1,
                                           navigation={"loaderId": "new-document"})
        methods = [command["method"] for command in ws.commands]
        self.assertEqual(methods.count("Page.getFrameTree"), 2)
        probes = [command for command in ws.commands if command["params"].get("expression") == bu.PAGE_STATE_JS]
        self.assertEqual(len(probes), 1)

    def test_real_adapter_classifies_pre_trigger_failures_as_retryable(self):
        first, second = bu.BrowserUpstream(target_hint="a"), MemoryTab("b")
        pool = seeded_pool(first, second)
        try:
            with patch.object(first, "_prepare_for_send", side_effect=bu.BrowserError("not initialized")), \
                 patch.object(first, "_eval") as evaluate:
                self.assertEqual(pool.generate({}, timeout=0.5), WIRE)
                evaluate.assert_not_called()  # Neither arm nor trigger reached the page.
        finally:
            pool.close()

    def test_stale_ready_tab_is_rejected_without_waiting_on_its_page(self):
        first, second = bu.BrowserUpstream(target_hint="a"), MemoryTab("b")
        first._pool_managed = True
        pool = seeded_pool(first, second)
        try:
            with patch.object(first, "_connect"), \
                 patch.object(first, "_eval", return_value=dict(READY, captcha_ready=False)) as evaluate, \
                 patch.object(first, "_prepare_for_send") as prepare:
                self.assertEqual(pool.generate({}, timeout=0.5), WIRE)
                prepare.assert_not_called()
                evaluate.assert_called_once_with(bu.PAGE_STATE_JS)
            self.assertEqual(second.sends, 1)
        finally:
            pool.close()

    def test_lost_trigger_acknowledgement_never_replays_the_request(self):
        first, second = bu.BrowserUpstream(target_hint="a"), MemoryTab("b")
        pool = seeded_pool(first, second)
        triggered = []

        def evaluate(expression, timeout=5):
            if expression == bu.TRIGGER_JS:
                triggered.append(True)
                raise bu.BrowserError("lost click acknowledgement")
            if "__bridgeAbort" in expression:
                return True

        try:
            with patch.object(first, "_prepare_for_send"), patch.object(first, "_eval", side_effect=evaluate):
                with self.assertRaisesRegex(bu.BrowserError, "lost click acknowledgement"):
                    pool.generate({}, timeout=0.5)
            self.assertEqual(triggered, [True])
            self.assertEqual(second.sends, 0)
        finally:
            pool.close()


class StreamingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_sse_stream_waits_with_heartbeats_then_delivers_content(self):
        pool = MemoryPool(size=1)
        gate = pool.gates["kpool=1"] = threading.Event()
        try:
            pool.start()
            with patch.multiple(bridge, _browser=pool, BROWSER_HEARTBEAT_SEC=0.02):
                output = []
                async with asyncio.timeout(2):
                    async for chunk in bridge.stream_anthropic({}, {}, "kimi-k3.1-pro"):
                        output.append(chunk)
                        if "event: ping" in chunk:
                            gate.set()
                wire = "".join(output)
            self.assertEqual(wire.count("event: message_start"), 1)
            self.assertIn("event: ping", wire)
            self.assertIn("RECOVERED", wire)
            self.assertIn("event: message_stop", wire)
            self.assertNotIn("event: error", wire)
            self.assertEqual(pool.tabs["kpool=1"].sends, 1)
        finally:
            pool.close()

    async def test_preparation_failure_is_hidden_inside_original_sse_stream(self):
        first, second = MemoryTab("a"), MemoryTab("b")
        first.preparation_failures = 1
        pool = seeded_pool(first, second)
        try:
            with patch.object(bridge, "_browser", pool):
                wire = "".join([chunk async for chunk in bridge.stream_anthropic({}, {}, "kimi-k3.1-pro")])
            self.assertEqual(wire.count("event: message_start"), 1)
            self.assertIn("RECOVERED", wire)
            self.assertIn("event: message_stop", wire)
            self.assertNotIn("event: error", wire)
            self.assertEqual((first.sends, second.sends), (0, 1))
        finally:
            pool.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
