"""The files of the repository agree with each other."""
import os
import re
import shutil
import subprocess
import unittest

import support
from support import otb


def read(*parts):
    with open(os.path.join(support.ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def shell_default(script, name):
    """The default of a variable set as NAME="${OTB_NAME:-default}"."""
    match = re.search(rf'^{name}="\$\{{OTB_{name}:-([^}}]+)\}}"$', script,
                      re.M)
    return match.group(1) if match else None


def setting_names(defaults, prefix=""):
    for key, value in defaults.items():
        if isinstance(value, dict):
            yield from setting_names(value, f"{prefix}{key}.")
        else:
            yield prefix + key


class ConsistencyTest(unittest.TestCase):
    def test_scripts_and_daemon_share_one_bundle_id(self):
        for script in ("install.sh", "uninstall.sh"):
            self.assertEqual(shell_default(read(script), "BUNDLE_ID"),
                             otb.LABEL, script)
            self.assertIn(f'APP_NAME="{otb.APP_NAME}"', read(script), script)

    def test_launcher_runs_the_file_that_install_puts_in_place(self):
        launcher = re.search(r'#define SCRIPT "([^"]+)"',
                             read("src", "launcher.c")).group(1)
        install = read("install.sh")
        libexec = re.search(r'^LIBEXEC="\$HOME(/[^"]+)"$', install,
                            re.M).group(1)
        self.assertEqual(launcher, libexec + "/outlook_telegram_bridge.py")
        self.assertIn('"$LIBEXEC/outlook_telegram_bridge.py"', install)

    def test_install_writes_where_the_daemon_reads(self):
        install = read("install.sh")
        config_dir = re.search(r'^CONFIG_DIR="\$HOME(/[^"]+)"$', install,
                               re.M).group(1)
        state_dir = re.search(r'^STATE_DIR="\$HOME(/[^"]+)"$', install,
                              re.M).group(1)
        self.assertEqual("~" + config_dir + "/config.json", otb.CONFIG_PATH)
        self.assertEqual("~" + state_dir, otb.STATE_DIR)

    def test_the_readme_explains_every_setting(self):
        readme = read("README.md")
        for name in setting_names(otb.DEFAULTS):
            self.assertIn(f"`{name}`", readme, name)

    def test_the_docs_name_every_command(self):
        for doc in ("README.md", "AGENTS.md"):
            text = read(doc)
            for option in ("--check", "--set-token", "--find-chat",
                           "--preview", "--selftest"):
                self.assertIn(option, text, f"{option} in {doc}")

    def test_agents_md_is_what_claude_md_loads(self):
        self.assertEqual(read("CLAUDE.md").strip(), "@AGENTS.md")

    def test_the_versions_in_install_sh_stay_fixed(self):
        # Changing them rebuilds the app, and macOS drops its Full Disk
        # Access with the old one.
        install = read("install.sh")
        self.assertIn("<key>CFBundleShortVersionString</key>"
                      "<string>1.0</string>", install)
        self.assertIn("<key>CFBundleVersion</key><string>1</string>", install)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not found")
    def test_shellcheck(self):
        subprocess.run(["shellcheck",
                        os.path.join(support.ROOT, "install.sh"),
                        os.path.join(support.ROOT, "uninstall.sh")],
                       check=True)

    def test_scripts_parse(self):
        for script in ("install.sh", "uninstall.sh"):
            subprocess.run(["bash", "-n", os.path.join(support.ROOT, script)],
                           check=True)


if __name__ == "__main__":
    unittest.main()
