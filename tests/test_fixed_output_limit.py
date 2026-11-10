"""An explicitly fixed browser cap must survive CC's smaller fallback requests."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import nvplay_bridge as bridge
import responses_support


class FixedOutputLimitTests(unittest.TestCase):
    def body(self, tokens=64000):
        return {"model": "kimi-k3.1-pro", "max_tokens": tokens,
                "output_config": {"effort": "max"},
                "messages": [{"role": "user", "content": "local protocol fixture"}]}

    def test_fixed_browser_cap_survives_cc_streaming_fallback(self):
        with patch.object(bridge, "UPSTREAM_KIND", "browser"), \
                patch.object(bridge, "BROWSER_MAX_TOKENS", 131072), \
                patch.object(bridge, "BROWSER_FIXED_MAX_TOKENS", 131072, create=True), \
                patch.object(bridge, "_dbg") as debug:
            for stream in (True, False):
                for requested in (128, 64000, 131072, 999999, None):
                    with self.subTest(stream=stream, requested=requested):
                        body = {**self.body(requested), "stream": stream}
                        payload = bridge.to_openai_request(body)
                        self.assertEqual(payload["max_tokens"], 131072)
                        self.assertEqual(payload["reasoning_effort"], "max")
            self.assertTrue(any(call.args[0] == "browser_output_limit_override"
                                and call.kwargs["requested_max_tokens"] == 64000
                                and call.kwargs["effective_max_tokens"] == 131072
                                for call in debug.call_args_list))

    def test_disabled_fixed_cap_preserves_client_limits(self):
        with patch.object(bridge, "UPSTREAM_KIND", "browser"), \
                patch.object(bridge, "BROWSER_MAX_TOKENS", 131072), \
                patch.object(bridge, "BROWSER_FIXED_MAX_TOKENS", 0, create=True):
            for requested in (128, 64000, 131072, 999999):
                self.assertEqual(bridge.to_openai_request(self.body(requested))["max_tokens"],
                                 min(requested, 131072))

    def test_browser_override_does_not_change_other_upstreams(self):
        with patch.object(bridge, "BROWSER_FIXED_MAX_TOKENS", 131072, create=True):
            for kind in ("openai", "nvcf"):
                with self.subTest(kind=kind), patch.object(bridge, "UPSTREAM_KIND", kind):
                    self.assertEqual(bridge.to_openai_request(self.body())["max_tokens"], 64000)

    def test_responses_uses_the_same_fixed_browser_output_cap(self):
        with patch.object(bridge, "UPSTREAM_KIND", "browser"), \
                patch.object(bridge, "BROWSER_FIXED_MAX_TOKENS", 131072):
            for stream in (True, False):
                payload, _, _ = responses_support.prepare_request({
                    "model": "kimi-k3.1-pro", "input": "local protocol fixture",
                    "max_output_tokens": 64000, "reasoning": {"effort": "high"},
                    "stream": stream}, bridge)
                self.assertEqual(payload["max_tokens"], 131072)
                self.assertEqual(payload["reasoning_effort"], "high")

    def test_invalid_fixed_cap_is_rejected_at_startup(self):
        for value in ("-1", "131073"):
            with self.subTest(value=value):
                result = subprocess.run([sys.executable, "-c", "import nvplay_bridge"],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "BROWSER_MAX_TOKENS": "131072", "BROWSER_FIXED_MAX_TOKENS": value},
                    capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BROWSER_FIXED_MAX_TOKENS", result.stderr)

    def test_health_exposes_the_effective_browser_cap_configuration(self):
        with patch.object(bridge, "UPSTREAM_KIND", "browser"), \
                patch.object(bridge, "BROWSER_MAX_TOKENS", 131072), \
                patch.object(bridge, "BROWSER_FIXED_MAX_TOKENS", 131072, create=True):
            self.assertEqual(asyncio.run(bridge.health())["browser_output_tokens"],
                             {"maximum": 131072, "fixed": 131072})

    def test_health_exposes_loaded_browser_deadlines(self):
        with patch.object(bridge, "UPSTREAM_KIND", "browser"), \
                patch.object(bridge, "BROWSER_TIMEOUT", 28800), \
                patch("browser_upstream.FIRST_BYTE_TIMEOUT", 3600.0), \
                patch("browser_upstream.IDLE_TIMEOUT", 3600.0):
            self.assertEqual(asyncio.run(bridge.health())["browser_timeouts"],
                {"total_seconds": 28800, "first_byte_seconds": 3600.0, "idle_seconds": 3600.0})


if __name__ == "__main__":
    unittest.main()
