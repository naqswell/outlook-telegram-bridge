"""--check: what it concludes from launchd and from the daemon's status."""
import contextlib
import io
import os
import tempfile
import time
import unittest
from unittest import mock

import support
from support import otb


class AgentLineTest(support.TempDirTest):
    """The background agent runs with the app's Full Disk Access, so only
    its own report says whether the app can read notifications."""

    def line(self, launchd, status=None, plist=False):
        state_dir = self.path("state")
        if status is not None:
            otb.write_json(os.path.join(state_dir, "status.json"), status)
        home = tempfile.mkdtemp(dir=self.tmp)
        if plist:
            path = os.path.join(home, "Library", "LaunchAgents",
                                f"{otb.LABEL}.plist")
            os.makedirs(os.path.dirname(path))
            open(path, "w").close()
        out = otb.Checklist()
        with mock.patch.object(otb, "launchd_state", return_value=launchd), \
                mock.patch.dict(os.environ, {"HOME": home}), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            otb.check_agent(out, state_dir)
        self.failed = out.failed
        return printed.getvalue().strip()

    def status(self, **fields):
        return dict({"pid": 501, "parent_pid": 500, "database": "ok",
                     "outlook": True, "delivery": None, "error": None,
                     "updated": time.time()}, **fields)

    def test_running_and_reading(self):
        line = self.line("running, pid 500", self.status())
        self.assertTrue(line.startswith("PASS  background agent: running, "
                                        "pid 500; it reads the notification "
                                        "database"), line)
        line = self.line("running, pid 500", self.status(delivery="ok"))
        self.assertTrue(line.endswith("and forwards to Telegram"), line)

    def test_no_full_disk_access_is_a_fail_with_the_fix(self):
        line = self.line("running, pid 500",
                         self.status(database="authorization denied",
                                     outlook=None))
        self.assertTrue(line.startswith("FAIL"), line)
        self.assertIn("authorization denied", line)
        self.assertIn("Full Disk Access", line)
        self.assertTrue(self.failed)

    def test_an_error_on_the_first_pass_is_a_fail(self):
        line = self.line("running, pid 500",
                         self.status(database=None, error="KeyError: 'x'"))
        self.assertTrue(line.startswith("FAIL"), line)
        self.assertIn("its last pass failed: KeyError", line)

    def test_an_agent_silent_for_long_may_be_stuck(self):
        line = self.line("running, pid 500",
                         self.status(updated=time.time() - 3600))
        self.assertTrue(line.startswith("WARN"), line)
        self.assertIn("has not reported for 60 minutes", line)
        self.assertIn("kickstart", line)

    def test_a_failing_delivery_is_a_fail(self):
        line = self.line("running, pid 500",
                         self.status(delivery="HTTP 403: Forbidden"))
        self.assertTrue(line.startswith("FAIL"), line)
        self.assertIn("HTTP 403", line)

    def test_outlook_silent_so_far_is_a_warning(self):
        line = self.line("running, pid 500", self.status(outlook=False))
        self.assertTrue(line.startswith("WARN"), line)
        self.assertIn("System Settings > Notifications", line)

    def test_a_report_from_an_earlier_run_does_not_count(self):
        for status in (None, self.status(pid=1, parent_pid=2),
                       self.status(database=None)):
            with self.subTest(status=status):
                line = self.line("running, pid 500", status)
                self.assertIn("has not reported yet", line)
                self.assertFalse(self.failed)

    def test_not_started_or_not_installed(self):
        self.assertIn("launchctl bootstrap gui/$(id -u)",
                      self.line("not loaded", plist=True))
        self.assertIn("not installed", self.line("not loaded"))
        self.assertIn("not available", self.line(None))

    def test_loaded_but_not_running_points_to_the_log(self):
        line = self.line("waiting")
        self.assertTrue(line.startswith("WARN"), line)
        self.assertIn("agent.log", line)


class SetTokenTest(support.TempDirTest):
    def test_a_loaded_agent_is_told_to_restart(self):
        api = support.FakeTelegram()
        self.addCleanup(api.close)
        path = support.write_config(self.path("config.json"), token=None,
                                    telegram={"api_url": api.url})
        with mock.patch.object(otb, "launchd_state",
                               return_value="running, pid 7"), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            code = otb.run_set_token(path, io.StringIO(support.TOKEN))
        self.assertEqual(code, 0)
        self.assertIn("launchctl kickstart -k", printed.getvalue())
        self.assertNotIn(support.TOKEN, printed.getvalue())


class DatabaseLineTest(support.TempDirTest):
    def lines(self, cfg):
        out = otb.Checklist()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            otb.check_database(out, cfg)
        self.failed = out.failed
        return printed.getvalue()

    def test_a_terminal_without_access_is_no_failure(self):
        cfg = otb.read_config(support.write_config(
            self.path("config.json"), db_path=self.path("absent", "db")))
        printed = self.lines(cfg)
        self.assertTrue(printed.startswith("INFO  notification database: "
                                           "this terminal cannot read"),
                        printed)
        self.assertIn("background agent line", printed)
        self.assertFalse(self.failed)

    def test_a_readable_database_shows_outlook(self):
        center = support.NotificationCenter(self.path("nc", "db"))
        self.addCleanup(center.close)
        center.post("Mail")
        cfg = otb.read_config(support.write_config(
            self.path("config.json"), db_path=center.path))
        printed = self.lines(cfg)
        self.assertIn("PASS  notification database: this terminal can read",
                      printed)
        self.assertIn("PASS  Outlook notifications: 1 stored", printed)


if __name__ == "__main__":
    unittest.main()
