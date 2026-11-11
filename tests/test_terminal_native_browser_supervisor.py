"""Browser recovery must preserve active sessions and the reboot boundary."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import supervise_terminal_native_browser as supervisor


class BrowserSupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.binary = self.base / "brave"
        self.binary.write_text("fixture")
        self.record = {"pid": 42, "boot_id": "boot", "start_ticks": "123", "started_at": 10,
                       "args": [str(self.binary), f"--user-data-dir={self.base}/brave-profile",
                                "--remote-debugging-port=9225", "--proxy-server=http://127.0.0.1:31128"]}
        for name, data in (("brave-process.json", self.record),
                           ("bridge-runtime.json", {"cdp_port": 9225}),
                           ("reboot-stop-request.json", {"stop_at": 200})):
            (self.base / name).write_text(json.dumps(data))

    def test_deadline_and_pause_prevent_any_network_or_restart(self):
        with patch.object(supervisor.time, "time", return_value=200), \
             patch.object(supervisor.LOCAL_HTTP, "open") as request, \
             patch.object(supervisor, "start_process") as start:
            self.assertEqual(supervisor.tick(self.base)["status"], "stopped_for_deadline_or_pause")
            request.assert_not_called()
            start.assert_not_called()
        (self.base / "pause-state.json").write_text("{}")
        self.assertFalse(supervisor.may_recover(self.base, 100))

    def test_campaign_without_deadline_still_recovers_exited_browser(self):
        (self.base / "reboot-stop-request.json").unlink()
        with patch.object(supervisor, "process_identity", return_value=None), \
             patch.object(supervisor.LOCAL_HTTP, "open", side_effect=OSError("CDP unavailable")), \
             patch.object(supervisor, "owner_environment", return_value={"HOME": "/ordinary-user"}), \
             patch.object(supervisor, "start_process", return_value=Mock(pid=43)) as start:
            self.assertEqual(supervisor.tick(self.base)["status"], "browser_restarted")
            start.assert_called_once()
        (self.base / "pause-state.json").write_text("{}")
        self.assertFalse(supervisor.may_recover(self.base, 100))

    def test_existing_stop_request_without_a_deadline_prevents_recovery(self):
        (self.base / "reboot-stop-request.json").write_text("{}")
        self.assertFalse(supervisor.may_recover(self.base, 100))

    def test_live_browser_with_unavailable_cdp_is_never_killed_or_replaced(self):
        identity = {key: self.record[key] for key in ("pid", "boot_id", "start_ticks")}
        with patch.object(supervisor.time, "time", return_value=100), \
             patch.object(supervisor, "process_identity", return_value=identity), \
             patch.object(supervisor.LOCAL_HTTP, "open", side_effect=OSError("CDP unavailable")), \
             patch.object(supervisor, "start_process") as start, patch.object(supervisor, "stop") as stop:
            self.assertEqual(supervisor.tick(self.base)["status"], "live_browser_cdp_unavailable")
            start.assert_not_called()
            stop.assert_not_called()

    def test_exited_browser_is_restarted_with_same_args_and_owner_environment(self):
        with patch.object(supervisor.time, "time", return_value=100), \
             patch.object(supervisor, "process_identity", return_value=None), \
             patch.object(supervisor.LOCAL_HTTP, "open", side_effect=OSError("CDP unavailable")), \
             patch.object(supervisor, "owner_environment", return_value={"HOME": "/ordinary-user"}), \
             patch.object(supervisor, "start_process", return_value=Mock(pid=43)) as start:
            row = supervisor.tick(self.base)
            self.assertEqual(row["status"], "browser_restarted")
            self.assertEqual(row["browser_pid"], 43)
            start.assert_called_once_with(self.base, "brave", self.record["args"],
                                          env={"HOME": "/ordinary-user"}, ordinary_user=True)
            self.assertEqual(json.loads((self.base / row["previous_record"]).read_text()), self.record)

    def test_reused_pid_and_foreign_profile_are_not_touched(self):
        with patch.object(supervisor.time, "time", return_value=100), \
             patch.object(supervisor, "process_identity", return_value={"pid": 42, "boot_id": "other", "start_ticks": "123"}), \
             patch.object(supervisor, "start_process") as start:
            with self.assertRaisesRegex(RuntimeError, "PID was reused"):
                supervisor.tick(self.base)
            start.assert_not_called()
        self.record["args"][1] = "--user-data-dir=/another/profile"
        with self.assertRaisesRegex(RuntimeError, "isolated profile"):
            supervisor.browser_command(self.base, self.record, 9225)

    def test_deadline_that_arrives_during_cdp_check_prevents_restart(self):
        with patch.object(supervisor.time, "time", side_effect=[100, 200, 200]), \
             patch.object(supervisor, "process_identity", return_value=None), \
             patch.object(supervisor.LOCAL_HTTP, "open", side_effect=OSError("CDP unavailable")), \
             patch.object(supervisor, "start_process") as start:
            self.assertEqual(supervisor.tick(self.base)["status"], "stopped_for_deadline_or_pause")
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
