"""Browser transport diagnostics stay bounded and omit request contents."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from monitor_cdp_network import PredictionRequests


class PredictionRequestTests(unittest.TestCase):
    def test_failed_prediction_preserves_protocol_status_and_network_error(self):
        recorder = PredictionRequests()
        events = [
            {"method": "Network.requestWillBeSent", "params": {
                "requestId": "a", "timestamp": 10, "request": {
                    "url": "https://example.test/v2/predict/models/a?token=secret",
                    "method": "POST", "headers": {"Authorization": "secret"}, "postData": "private prompt"}}},
            {"method": "Network.responseReceived", "params": {"requestId": "a", "response": {
                "status": 200, "protocol": "h2", "mimeType": "text/event-stream", "headers": {"Cookie": "secret"}}}},
            {"method": "Network.loadingFailed", "params": {
                "requestId": "a", "timestamp": 33.4, "errorText": "net::ERR_HTTP2_PROTOCOL_ERROR", "canceled": False}},
        ]
        output = [recorder.feed(event) for event in events]
        self.assertEqual(output[-1]["event"], "prediction_failed")
        self.assertEqual(output[-1]["elapsed_seconds"], 23.4)
        self.assertEqual(output[-1]["status"], 200)
        self.assertEqual(output[-1]["protocol"], "h2")
        self.assertEqual(output[-1]["errorText"], "net::ERR_HTTP2_PROTOCOL_ERROR")
        self.assertNotIn("secret", json.dumps(output))
        self.assertNotIn("private prompt", json.dumps(output))
        self.assertEqual(recorder.requests, {})

    def test_irrelevant_requests_are_ignored_and_inflight_metadata_is_bounded(self):
        recorder = PredictionRequests(maximum=2)
        self.assertIsNone(recorder.feed({"method": "Network.requestWillBeSent", "params": {
            "requestId": "ignored", "request": {"url": "https://example.test/favicon.ico"}}}))
        for request_id in ("a", "b", "c"):
            recorder.feed({"method": "Network.requestWillBeSent", "params": {
                "requestId": request_id, "timestamp": 1,
                "request": {"url": "https://example.test/v2/predict/models/a"}}})
        self.assertEqual(set(recorder.requests), {"b", "c"})
        self.assertIsNone(recorder.feed({"method": "Network.loadingFailed", "params": {"requestId": "a"}}))
        result = recorder.feed({"method": "Network.loadingFinished", "params": {
            "requestId": "b", "timestamp": 5, "encodedDataLength": 1024}})
        self.assertEqual(result["encoded_bytes"], 1024)
        self.assertEqual(result["elapsed_seconds"], 4)


if __name__ == "__main__":
    unittest.main()
