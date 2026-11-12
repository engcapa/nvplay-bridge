"""CDP failures retain their type and identify the failed connection/command."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

import browser_upstream as browser
import websocket


class CDPErrorContextTests(unittest.TestCase):
    def test_send_timeout_identifies_command_and_is_not_replayed(self):
        for pooled in (False, True):
            with self.subTest(pooled=pooled):
                error = websocket.WebSocketTimeoutException("send timed out")
                ws = Mock()
                ws.send.side_effect = error
                with self.assertRaises(websocket.WebSocketTimeoutException) as caught:
                    if pooled:
                        browser.BrowserPool._call(ws, 1, "Page.navigate", {"url": "about:blank"})
                    else:
                        upstream = browser.BrowserUpstream()
                        upstream.ws = ws
                        upstream._call("Runtime.evaluate", {"expression": "42"})
                self.assertIs(caught.exception, error)
                self.assertEqual(error.cdp_phase, "send")
                self.assertEqual(error.cdp_method, "Page.navigate" if pooled else "Runtime.evaluate")
                self.assertEqual(ws.send.call_count, 1)
                ws.recv.assert_not_called()

    def test_receive_timeout_preserves_exception_for_both_cdp_callers(self):
        for pooled in (False, True):
            with self.subTest(pooled=pooled):
                error = websocket.WebSocketTimeoutException("Connection timed out")
                ws = Mock()
                ws.recv.side_effect = error
                with self.assertRaises(websocket.WebSocketTimeoutException) as caught:
                    if pooled:
                        browser.BrowserPool._call(ws, 1, "Page.navigate", {"url": "about:blank"})
                    else:
                        upstream = browser.BrowserUpstream()
                        upstream.ws = ws
                        upstream._call("Runtime.evaluate", {"expression": "42"})
                self.assertIs(caught.exception, error)
                self.assertEqual(str(error), "Connection timed out")
                self.assertEqual(error.cdp_phase, "receive")
                self.assertEqual(error.cdp_method, "Page.navigate" if pooled else "Runtime.evaluate")

    def test_handshake_timeout_reaches_pool_error_with_its_original_type(self):
        error = websocket.WebSocketTimeoutException("Connection timed out")
        pool = browser.BrowserPool(size=1)
        pool._list_pages = Mock(return_value=[])
        pool._new_blank = Mock(return_value={"webSocketDebuggerUrl": "ws://local.test/fixture"})
        with patch.object(browser.websocket, "create_connection", side_effect=error):
            with self.assertRaises(websocket.WebSocketTimeoutException) as caught:
                pool._refresh_tab(browser.BrowserUpstream(), threading.Event(), time.monotonic() + 10)
        self.assertIs(caught.exception, error)
        self.assertEqual(error.cdp_phase, "connect")
        self.assertIsNone(getattr(error, "cdp_method", None))


if __name__ == "__main__":
    unittest.main()
