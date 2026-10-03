"""install.sh, uninstall.sh and the launcher, in a scratch home folder.

These run only on macOS with the Command Line Tools. They install under a
bundle id of their own, so a real installation on the same Mac is left
alone."""
import json
import os
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import unittest

import support
from support import otb

ON_MAC = sys.platform == "darwin" and all(
    shutil.which(tool) for tool in ("clang", "codesign", "plutil"))
TEST_BUNDLE_ID = otb.LABEL + ".test"


def macho(path):
    with open(path, "rb") as f:
        return f.read(4) in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


@unittest.skipUnless(ON_MAC, "needs macOS with the Command Line Tools")
class InstallTest(support.TempDirTest):
    def setUp(self):
        super().setUp()
        self.home = self.path("home")
        self.apps = self.path("Applications")
        os.makedirs(self.home)
        self.env = {"HOME": self.home, "OTB_APP_DIR": self.apps,
                    "OTB_BUNDLE_ID": TEST_BUNDLE_ID, "no_proxy": "*",
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
        self.app = os.path.join(self.apps, "OutlookTelegramBridge.app")
        self.exe = os.path.join(self.app, "Contents", "MacOS",
                                "OutlookTelegramBridge")
        self.libexec = os.path.join(self.home, ".local", "libexec",
                                    "outlook-telegram-bridge")
        self.config_path = os.path.join(self.home, ".config",
                                        "outlook-telegram-bridge",
                                        "config.json")
        self.state_dir = os.path.join(self.home, ".local", "state",
                                      "outlook-telegram-bridge")
        self.plist = os.path.join(self.home, "Library", "LaunchAgents",
                                  TEST_BUNDLE_ID + ".plist")

    def script(self, name, *args, root=support.ROOT):
        result = subprocess.run(["/bin/bash", os.path.join(root, name), *args],
                                env=self.env, capture_output=True, text=True,
                                timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def cdhash(self):
        shown = subprocess.run(["codesign", "-dvvv", self.app],
                               capture_output=True, text=True).stderr
        return re.search(r"CDHash=(\w+)", shown).group(1)

    def test_install_puts_everything_in_place(self):
        output = self.script("install.sh")
        with open(os.path.join(self.libexec, "outlook_telegram_bridge.py"),
                  "rb") as installed, open(support.SCRIPT, "rb") as source:
            self.assertEqual(installed.read(), source.read())
        self.assertEqual(stat.S_IMODE(os.stat(self.config_path).st_mode),
                         0o600)
        for private in (os.path.dirname(self.config_path), self.state_dir):
            self.assertEqual(stat.S_IMODE(os.stat(private).st_mode), 0o700)
        with open(os.path.join(self.app, "Contents", "Info.plist"),
                  "rb") as f:
            info = plistlib.load(f)
        self.assertEqual(info["CFBundleIdentifier"], TEST_BUNDLE_ID)
        self.assertEqual(info["CFBundleExecutable"], "OutlookTelegramBridge")
        self.assertTrue(macho(self.exe))
        subprocess.run(["codesign", "--verify", "--strict", self.app],
                       check=True)
        with open(self.plist, "rb") as f:
            agent = plistlib.load(f)
        self.assertEqual(agent["Label"], TEST_BUNDLE_ID)
        self.assertEqual(agent["ProgramArguments"], [self.exe])
        self.assertTrue(agent["RunAtLoad"] and agent["KeepAlive"])
        self.assertEqual(agent["StandardOutPath"],
                         os.path.join(self.state_dir, "agent.log"))
        self.assertIn(f"app:     {self.app}", output)
        self.assertIn("not started", output)
        self.assertIn("AGENTS.md", output)

    def test_paths_with_xml_characters_make_valid_plists(self):
        self.home = self.path("Tom & Jerry <home>")
        os.makedirs(self.home)
        self.env["HOME"] = self.home
        self.script("install.sh")
        plist = os.path.join(self.home, "Library", "LaunchAgents",
                             TEST_BUNDLE_ID + ".plist")
        with open(plist, "rb") as f:
            agent = plistlib.load(f)
        self.assertEqual(agent["StandardOutPath"], os.path.join(
            self.home, ".local", "state", "outlook-telegram-bridge",
            "agent.log"))

    def test_installing_again_keeps_the_app_and_its_permission(self):
        self.script("install.sh")
        before = (self.cdhash(), os.stat(self.exe).st_ino)
        output = self.script("install.sh")
        self.assertIn("unchanged", output)
        self.assertEqual((self.cdhash(), os.stat(self.exe).st_ino), before)

    def test_a_changed_launcher_replaces_the_app_and_says_so(self):
        repo = self.path("repo")
        shutil.copytree(support.ROOT, repo,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        self.script("install.sh", root=repo)
        with open(os.path.join(repo, "src", "launcher.c"), "a") as f:
            f.write("\n/* changed */\n")
        output = self.script("install.sh", root=repo)
        self.assertIn("grant it again", output)
        subprocess.run(["codesign", "--verify", "--strict", self.app],
                       check=True)

    def test_an_existing_config_is_kept_and_locked_down(self):
        self.script("install.sh")
        with open(self.config_path, "w") as f:
            f.write('{"telegram": {"chat_id": "mine"}}')
        os.chmod(self.config_path, 0o644)
        self.assertIn("kept the existing config", self.script("install.sh"))
        with open(self.config_path) as f:
            self.assertEqual(f.read(), '{"telegram": {"chat_id": "mine"}}')
        self.assertEqual(stat.S_IMODE(os.stat(self.config_path).st_mode),
                         0o600)

    def test_the_installed_app_runs_the_bridge(self):
        self.script("install.sh")
        version = subprocess.run([self.exe, "--version"], env=self.env,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertIn(otb.__version__, version.stdout)

        api = support.FakeTelegram()
        self.addCleanup(api.close)
        center = support.NotificationCenter(self.path("nc", "db"))
        self.addCleanup(center.close)
        support.write_config(self.config_path, db_path=center.path,
                             telegram={"api_url": api.url})
        check = subprocess.run([self.exe, "--check"], env=self.env,
                               capture_output=True, text=True, timeout=60)
        self.assertIn("PASS  Telegram bot: @mail_test_bot", check.stdout)
        once = [self.exe, "--once"]
        subprocess.run(once, env=self.env, check=True, timeout=60,
                       capture_output=True)  # the first run notes the time
        center.post("Through the app")
        subprocess.run(once, env=self.env, check=True, timeout=60,
                       capture_output=True)
        [fields] = api.sent()
        self.assertIn("Through the app", fields["text"])

    def test_uninstall_keeps_the_config_unless_purged(self):
        self.script("install.sh")
        os.makedirs(self.state_dir, exist_ok=True)
        self.script("uninstall.sh")
        for gone in (self.app, self.plist, self.libexec):
            self.assertFalse(os.path.exists(gone), gone)
        self.assertTrue(os.path.exists(self.config_path))
        self.assertTrue(os.path.isdir(self.state_dir))
        self.script("uninstall.sh", "--purge")
        self.assertFalse(os.path.exists(self.config_path))
        self.assertFalse(os.path.exists(self.state_dir))


@unittest.skipUnless(ON_MAC, "needs macOS with the Command Line Tools")
class LauncherTest(support.TempDirTest):
    """The launcher on its own, starting a stand-in for the daemon."""

    STAND_IN = """import json, os, signal, sys, time
out = os.environ["STAND_IN_OUT"]
def write(name, value):
    with open(os.path.join(out, name), "w") as f:
        f.write(value)
write("pid", str(os.getpid()))
write("seen", json.dumps({"args": sys.argv[1:],
                          "via_app": os.environ.get("OTB_VIA_APP")}))
if sys.argv[1:] == ["--wait"]:
    signal.signal(signal.SIGTERM,
                  lambda *a: (write("term", "yes"), sys.exit(0)))
    write("ready", "yes")
    while True:
        time.sleep(0.1)
sys.exit(int(os.environ.get("STAND_IN_EXIT", "0")))
"""

    def setUp(self):
        super().setUp()
        self.launcher = self.path("launcher")
        subprocess.run(["clang", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-o", self.launcher,
                        os.path.join(support.ROOT, "src", "launcher.c")],
                       check=True)
        self.home = self.path("home")
        self.out = self.path("out")
        os.makedirs(self.out)
        libexec = os.path.join(self.home, ".local", "libexec",
                               "outlook-telegram-bridge")
        os.makedirs(libexec)
        with open(os.path.join(libexec, "outlook_telegram_bridge.py"),
                  "w") as f:
            f.write(self.STAND_IN)
        self.env = {"HOME": self.home, "STAND_IN_OUT": self.out,
                    "PATH": "/usr/bin:/bin"}

    def read(self, name):
        with open(os.path.join(self.out, name)) as f:
            return f.read()

    def test_passes_arguments_and_exit_status_through(self):
        args = ["--preview", "2", "with space", "письмо"]
        result = subprocess.run([self.launcher, *args],
                                env=dict(self.env, STAND_IN_EXIT="7"),
                                timeout=60)
        self.assertEqual(result.returncode, 7)
        seen = json.loads(self.read("seen"))
        self.assertEqual(seen, {"args": args, "via_app": "1"})

    def test_passes_sigterm_on_and_leaves_no_orphan(self):
        process = subprocess.Popen([self.launcher, "--wait"], env=self.env)
        deadline = time.monotonic() + 30
        while not os.path.exists(os.path.join(self.out, "ready")):
            self.assertLess(time.monotonic(), deadline, "never got ready")
            time.sleep(0.05)
        child = int(self.read("pid"))
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=30), 0)
        self.assertEqual(self.read("term"), "yes")
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_passes_sighup_and_sigint_on(self):
        for sig, code in ((signal.SIGHUP, 128 + signal.SIGHUP),
                          (signal.SIGINT, 128 + signal.SIGINT)):
            with self.subTest(signal=sig.name):
                ready = os.path.join(self.out, "ready")
                if os.path.exists(ready):
                    os.remove(ready)
                process = subprocess.Popen([self.launcher, "--wait"],
                                           env=self.env,
                                           stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 30
                while not os.path.exists(ready):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.05)
                process.send_signal(sig)
                self.assertEqual(process.wait(timeout=30), code)

    def test_a_missing_daemon_is_named(self):
        shutil.rmtree(os.path.join(self.home, ".local"))
        result = subprocess.run([self.launcher], env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 78)
        self.assertIn("outlook_telegram_bridge.py", result.stderr)
        self.assertIn("install.sh", result.stderr)


if __name__ == "__main__":
    unittest.main()
