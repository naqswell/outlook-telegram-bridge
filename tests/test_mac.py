"""The parts that talk to macOS, with the system commands replaced."""
import contextlib
import io
import itertools
import re
import subprocess
import unittest
from unittest import mock

import support
from support import otb


class Run:
    """Stands in for subprocess.run: records commands, answers from a list."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        answer = self.answers.pop(0) if self.answers else ("", 0)
        if isinstance(answer, BaseException):
            raise answer
        stdout, code = answer
        return subprocess.CompletedProcess(command, code, stdout, "boom")


class DefocusTest(unittest.TestCase):
    CFG = {"defocus_outlook": {"enabled": True, "idle_seconds": 180,
                               "activate": "Finder"},
           "outlook_bundle_id": "com.microsoft.outlook"}

    def nudge(self, front, idle, cfg=None, run=None):
        self.run = run or Run()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            moved = otb.nudge_focus_off_outlook(
                cfg or self.CFG, frontmost=lambda: front, idle=lambda: idle,
                run=self.run)
        self.log = out.getvalue()
        return moved

    def test_idle_in_outlook_brings_another_app_forward(self):
        self.assertTrue(self.nudge("com.microsoft.Outlook", 600))
        self.assertEqual(self.run.commands, [["/usr/bin/open", "-a", "Finder"]])
        self.assertIn("600s", self.log)

    def test_it_stays_out_of_the_way_otherwise(self):
        for front, idle in (("com.microsoft.Outlook", 5),
                            ("com.apple.finder", 600),
                            ("com.microsoft.Outlook", None),
                            (None, 600)):
            with self.subTest(front=front, idle=idle):
                self.assertFalse(self.nudge(front, idle))
                self.assertEqual(self.run.commands, [])

    def test_disabled_does_nothing(self):
        cfg = dict(self.CFG, defocus_outlook={"enabled": False,
                                              "idle_seconds": 180,
                                              "activate": "Finder"})
        self.assertFalse(self.nudge("com.microsoft.Outlook", 600, cfg))
        self.assertEqual(self.run.commands, [])

    def test_a_failing_open_is_reported_not_claimed(self):
        self.assertFalse(self.nudge("com.microsoft.Outlook", 600,
                                    run=Run(("", 1))))
        self.assertIn("could not bring Finder forward", self.log)
        self.assertFalse(self.nudge("com.microsoft.Outlook", 600,
                                    run=Run(OSError("no open"))))


class ProbeTest(unittest.TestCase):
    def test_frontmost_bundle_id(self):
        run = Run(('"LSASN:{hi=0x0;lo=0x5a05a}"\n', 0),
                  ('"CFBundleIdentifier"="com.microsoft.Outlook"\n', 0))
        self.assertEqual(otb.frontmost_bundle_id(run), "com.microsoft.Outlook")
        self.assertEqual(run.commands[1][-1], '"LSASN:{hi=0x0;lo=0x5a05a}"')
        self.assertIsNone(otb.frontmost_bundle_id(Run(("", 0))))
        self.assertIsNone(otb.frontmost_bundle_id(Run(OSError())))
        self.assertIsNone(otb.frontmost_bundle_id(
            Run(subprocess.TimeoutExpired("lsappinfo", 10))))

    def test_input_idle_seconds(self):
        ioreg = '    | |   "HIDIdleTime" = 181000000000\n'
        self.assertEqual(otb.input_idle_seconds(Run((ioreg, 0))), 181.0)
        self.assertIsNone(otb.input_idle_seconds(Run(("nothing", 0))))
        self.assertIsNone(otb.input_idle_seconds(Run(OSError())))

    def test_telegram_desktop_pattern(self):
        run = Run(("", 0))
        self.assertTrue(otb.telegram_desktop_running(run))
        pattern = run.commands[0][-1]
        for path in ("/Applications/Telegram.app/Contents/MacOS/Telegram",
                     "/Applications/Telegram Desktop.app/Contents/MacOS/"
                     "Telegram"):
            self.assertTrue(re.search(pattern, path), path)
        self.assertFalse(re.search(pattern, "/usr/bin/telegram-cli"))
        self.assertFalse(otb.telegram_desktop_running(Run(("", 1))))
        self.assertFalse(otb.telegram_desktop_running(Run(OSError())))

    def test_launchd_state(self):
        printed = ("gui/501/x = {\n\tactive count = 1\n\tstate = running\n"
                   "\tpid = 4711\n\tendpoints = {\n\t\tstate = active\n")
        self.assertEqual(otb.launchd_state(Run((printed, 0))),
                         "running, pid 4711")
        self.assertEqual(otb.launchd_state(Run(("", 113))), "not loaded")
        self.assertEqual(otb.launchd_state(Run((
            "x = {\n\tstate = not running\n\tlast exit code = 1\n", 0))),
            "not running")
        self.assertIsNone(otb.launchd_state(Run(OSError())))


class WaitForConfigTest(support.TempDirTest):
    def test_waits_for_a_fix_and_reports_each_problem_once(self):
        path = support.write_config(self.path("config.json"),
                                    notify_content="loud")
        naps = []

        def sleep(seconds):
            naps.append(seconds)
            if len(naps) == otb.CONFIG_RETRY + 3:
                support.write_config(path)

        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(otb, "orphaned", return_value=False):
            cfg = otb.wait_for_config(path, sleep=sleep)
        self.assertEqual(cfg["notify_content"], "full")
        self.assertEqual(naps, [1] * otb.CONFIG_RETRY * 2)
        self.assertEqual(out.getvalue().count("config problem"), 1)

    def test_an_orphan_stops_waiting(self):
        path = support.write_config(self.path("config.json"),
                                    notify_content="loud")
        answers = itertools.chain([False, False], itertools.repeat(True))
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(otb, "orphaned",
                                  lambda: next(answers)):
            self.assertIsNone(otb.wait_for_config(path, sleep=lambda s: None))
        self.assertIn("is gone; stopping", out.getvalue())


class RunLoopTest(support.TempDirTest):
    def test_backoff_and_unexpected_errors_reach_the_status(self):
        center = support.NotificationCenter(self.path("nc", "db"))
        self.addCleanup(center.close)
        cfg = self.config(db_path=center.path, poll_seconds=5)
        state = self.path("state", "state.json")
        status_path = self.path("state", "status.json")
        naps, errors = [], []

        def sleep(seconds):
            naps.append(seconds)
            errors.append(otb.read_status(status_path)["error"])
            if len(naps) == 4:
                raise otb.Stop()

        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(otb.time, "sleep", sleep), \
                mock.patch.object(otb, "orphaned", return_value=False), \
                mock.patch.object(otb, "telegram_desktop_running",
                                  return_value=False), \
                mock.patch.object(otb.Bridge, "poll", side_effect=[
                    False, False, RuntimeError("boom"), True]):
            with self.assertRaises(otb.Stop):
                otb.run_loop(cfg, state)
        self.assertEqual(naps, [10, 20, 5, 5])
        self.assertEqual(errors, [None, None, "RuntimeError: boom", None])
        self.assertEqual(out.getvalue().count("unexpected error"), 1)

    def test_status_is_rewritten_now_and_then_even_unchanged(self):
        center = support.NotificationCenter(self.path("nc", "db"))
        self.addCleanup(center.close)
        cfg = self.config(db_path=center.path)
        status_path = self.path("state", "status.json")
        stamps = []

        def sleep(seconds):
            stamps.append(otb.read_status(status_path)["updated"])
            if len(stamps) == 3:
                raise otb.Stop()

        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(otb, "STATUS_EVERY", 0), \
                mock.patch.object(otb.time, "sleep", sleep), \
                mock.patch.object(otb, "orphaned", return_value=False), \
                mock.patch.object(otb, "telegram_desktop_running",
                                  return_value=False):
            with self.assertRaises(otb.Stop):
                otb.run_loop(cfg, self.path("state", "state.json"))
        self.assertEqual(len(set(stamps)), 3)

    def test_a_long_poll_interval_is_kept(self):
        center = support.NotificationCenter(self.path("nc", "db"))
        self.addCleanup(center.close)
        cfg = self.config(db_path=center.path, poll_seconds=600)
        naps = []

        def sleep(seconds):
            naps.append(seconds)
            raise otb.Stop()

        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(otb.time, "sleep", sleep), \
                mock.patch.object(otb, "orphaned", return_value=False), \
                mock.patch.object(otb, "telegram_desktop_running",
                                  return_value=False):
            with self.assertRaises(otb.Stop):
                otb.run_loop(cfg, self.path("state", "state.json"))
        self.assertEqual(naps, [600])


if __name__ == "__main__":
    unittest.main()
