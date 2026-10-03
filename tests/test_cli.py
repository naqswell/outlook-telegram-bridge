"""The bridge run as a program, against a stand-in database and Bot API."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest

import support
from support import otb


def wait_until(condition, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


class CliTest(support.TempDirTest):
    def setUp(self):
        super().setUp()
        self.api = support.FakeTelegram()
        self.addCleanup(self.api.close)
        self.center = support.NotificationCenter(
            self.path("Group Containers", "db"))
        self.addCleanup(self.center.close)
        self.config_path = self.path("config", "config.json")
        os.makedirs(os.path.dirname(self.config_path))
        self.write_config()
        home = self.path("home")
        os.makedirs(home)
        self.env = dict(os.environ, HOME=home, OTB_CONFIG=self.config_path,
                        OTB_STATE_DIR=self.path("state"), no_proxy="*")
        self.env.pop("OTB_VIA_APP", None)

    def write_config(self, token=support.TOKEN, **settings):
        settings = otb.merge({"db_path": self.center.path, "poll_seconds": 0.1,
                              "telegram": {"api_url": self.api.url}}, settings)
        support.write_config(self.config_path, token, **settings)

    def bridge(self, *args, stdin=""):
        return subprocess.run([sys.executable, support.SCRIPT, *args],
                              env=self.env, capture_output=True, text=True,
                              input=stdin, timeout=60)

    def start_daemon(self):
        process = subprocess.Popen(
            [sys.executable, support.SCRIPT], env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        if process.stdout.closed:
            return ""
        try:
            output, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        return output

    def texts(self):
        return [fields["text"] for fields in self.api.sent()]

    def test_version_and_help(self):
        self.assertIn(otb.__version__, self.bridge("--version").stdout)
        usage = self.bridge("--help").stdout
        for option in ("--check", "--find-chat", "--preview", "--selftest",
                       "--once"):
            self.assertIn(option, usage)

    def test_once_forwards_what_is_new_since_the_first_run(self):
        self.center.post("Before the bridge", at=support.mac_now() - 60)
        first = self.bridge("--once")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.texts(), [])
        self.center.post("Budget", "Alex Smith", "Numbers inside")
        second = self.bridge("--once")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.texts(), [
            "\U0001F4E7 Alex Smith · Budget · Numbers inside"])
        with open(self.path("state", "state.json")) as f:
            state = json.load(f)
        self.assertEqual(len(state["seen"]), 2)
        self.assertIsNotNone(state["last"])

    def test_once_reports_a_failed_delivery_and_retries_next_time(self):
        self.bridge("--once")
        self.center.post("Budget")
        self.api.reply("sendMessage", 500, support.error(500, "Internal"))
        failed = self.bridge("--once")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("HTTP 500", failed.stdout)
        self.assertEqual(self.bridge("--once").returncode, 0)
        self.assertEqual(len(self.texts()), 2)

    def test_selftest_sends_one_message(self):
        result = self.bridge("--selftest")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [text] = self.texts()
        self.assertIn("bridge test", text)
        self.assertIn("test message sent to chat 42", result.stdout)
        self.assertIn("your phone showed a notification", result.stdout)

    def test_selftest_does_not_hide_a_wrong_topic(self):
        self.write_config(telegram={"chat_id": "-100777", "thread_id": 9})
        self.api.reply("sendMessage", 400, support.error(
            400, "Bad Request: message thread not found"))
        result = self.bridge("--selftest")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len(self.api.sent()), 1)
        self.assertIn("chat -100777, topic 9 failed", result.stdout)
        self.assertIn("check telegram.thread_id", result.stdout)

    def test_set_token_saves_it_without_showing_it(self):
        token_file = otb.token_path(self.config_path)
        os.remove(token_file)
        result = self.bridge("--set-token", stdin=support.TOKEN + "\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Saved the token of @mail_test_bot", result.stdout)
        self.assertNotIn(support.TOKEN, result.stdout + result.stderr)
        self.assertEqual(otb.read_token(token_file), support.TOKEN)
        self.assertEqual(os.stat(token_file).st_mode & 0o777, 0o600)
        self.assertEqual(self.api.calls_of("getMe")[0]["token"], support.TOKEN)

    def test_set_token_refuses_what_is_not_a_token(self):
        token_file = otb.token_path(self.config_path)
        os.remove(token_file)
        for typed in ("my password\n", "", "123:short\n"):
            with self.subTest(typed=typed):
                result = self.bridge("--set-token", stdin=typed)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Nothing was saved", result.stdout)
                if typed.strip():
                    self.assertNotIn(typed.strip(), result.stdout)
                self.assertFalse(os.path.exists(token_file))

    def test_set_token_refuses_a_token_telegram_rejects(self):
        token_file = otb.token_path(self.config_path)
        os.remove(token_file)
        other = "999999:AAE_other-token-0123456789abcdefgh"
        result = self.bridge("--set-token", stdin=other)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not know this token", result.stdout)
        self.assertNotIn(other, result.stdout)
        self.assertFalse(os.path.exists(token_file))

    def test_set_token_offline_saves_and_says_it_is_unconfirmed(self):
        self.write_config(token=None, telegram={"api_url": "http://127.0.0.1:9"})
        result = self.bridge("--set-token", stdin=support.TOKEN)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("could not confirm", result.stdout)
        self.assertIn("telegram.proxy", result.stdout)
        self.assertEqual(otb.read_token(otb.token_path(self.config_path)),
                         support.TOKEN)

    def test_selftest_explains_a_failure(self):
        self.api.reply("sendMessage", 403, support.error(
            403, "Forbidden: bot was blocked by the user"))
        result = self.bridge("--selftest")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unblock", result.stdout)

    def test_check_passes_on_a_working_setup(self):
        result = self.bridge("--check")
        expected_code = 0 if sys.platform == "darwin" else 1
        self.assertEqual(result.returncode, expected_code, result.stdout)
        for line in ("PASS  config", "PASS  notification database",
                     "PASS  Outlook notifications: ",
                     "PASS  Telegram bot: @mail_test_bot",
                     "PASS  Telegram chat: private Alex (@alex)"):
            self.assertIn(line, result.stdout)
        self.assertEqual(self.texts(), [])

    def test_check_names_a_wrong_token_without_showing_it(self):
        self.write_config(token="999999:AAE_wrong-token-0123456789abcdefgh")
        result = self.bridge("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  Telegram bot: HTTP 401", result.stdout)
        self.assertIn("@BotFather", result.stdout)
        self.assertNotIn("wrong-token", result.stdout)

    def test_check_goes_on_past_config_problems(self):
        self.write_config(notify_content="loud", telegram={"chat_id": ""})
        result = self.bridge("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  config: notify_content", result.stdout)
        self.assertIn("FAIL  config: telegram.chat_id", result.stdout)
        self.assertIn("PASS  notification database", result.stdout)
        self.assertIn("PASS  Telegram bot", result.stdout)

    def test_check_names_a_terminal_proxy_the_bridge_ignores(self):
        self.env["HTTPS_PROXY"] = "http://user:secret@127.0.0.1:9"
        result = self.bridge("--check")
        self.assertIn("INFO  Telegram proxy: this terminal sets HTTPS_PROXY",
                      result.stdout)
        self.assertNotIn("secret", result.stdout)
        self.assertIn("PASS  Telegram bot", result.stdout)
        self.write_config(telegram={"proxy": "http://127.0.0.1:9"})
        self.assertNotIn("Telegram proxy:", self.bridge("--check").stdout)

    def test_set_token_says_when_nothing_came_in(self):
        os.remove(otb.token_path(self.config_path))
        result = self.bridge("--set-token", stdin="")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Nothing came in", result.stdout)

    def test_check_reports_a_missing_config(self):
        self.env["OTB_CONFIG"] = self.path("absent.json")
        result = self.bridge("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  config: no config at", result.stdout)

    def test_check_warns_about_a_token_others_can_read(self):
        os.chmod(otb.token_path(self.config_path), 0o644)
        self.assertIn("WARN  config: other users can read the bot token",
                      self.bridge("--check").stdout)

    def test_check_asks_for_a_token(self):
        self.write_config(token=None)
        result = self.bridge("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  config: no bot token yet", result.stdout)
        self.assertNotIn("Telegram bot:", result.stdout)

    def test_check_finds_a_bot_outside_the_group(self):
        self.api.reply("getChat", 200, {"ok": True, "result": {
            "id": -100777, "type": "supergroup", "title": "Mail",
            "is_forum": True}})
        self.api.reply("getChatMember", 200, {"ok": True, "result": {
            "status": "left"}})
        self.write_config(telegram={"chat_id": "-100777", "thread_id": 2})
        result = self.bridge("--check")
        self.assertIn('PASS  Telegram chat: supergroup "Mail"', result.stdout)
        self.assertIn("FAIL  Telegram chat: the bot is not a member",
                      result.stdout)
        self.assertIn("INFO  Telegram topic: thread_id 2", result.stdout)

    def test_check_knows_what_a_bot_may_do_in_channels_and_groups(self):
        for chat, member, fragment in (
                ({"id": -100888, "type": "channel", "title": "News"},
                 {"status": "member"}, "only as an administrator"),
                ({"id": -100999, "type": "supergroup", "title": "Mail"},
                 {"status": "restricted", "can_send_messages": False},
                 "may not send messages")):
            with self.subTest(chat=chat["type"]):
                self.write_config(telegram={"chat_id": str(chat["id"])})
                self.api.reply("getChat", 200, {"ok": True, "result": chat})
                self.api.reply("getChatMember", 200,
                               {"ok": True, "result": member})
                result = self.bridge("--check")
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"FAIL  Telegram chat: ", result.stdout)
                self.assertIn(fragment, result.stdout)

    def test_set_token_finds_the_token_in_the_whole_botfather_message(self):
        os.remove(otb.token_path(self.config_path))
        message = ("Done! Congratulations on your new bot.\n\n"
                   "Use this token to access the HTTP API:\n"
                   f"{support.TOKEN}\n"
                   "Keep your token secure, it can be used by anyone.\n"
                   "For a description of the Bot API, see this page: "
                   "https://core.telegram.org/bots/api\n")
        result = self.bridge("--set-token", stdin=message)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(otb.read_token(otb.token_path(self.config_path)),
                         support.TOKEN)

    def test_find_chat_lists_who_wrote_to_the_bot(self):
        self.write_config(telegram={"chat_id": ""})
        self.api.reply("getUpdates", 200, {"ok": True, "result": [
            {"update_id": 1, "message": {
                "message_id": 5, "text": "hi",
                "chat": {"id": 31337, "type": "private", "first_name": "Alex",
                         "username": "alex"}}},
            {"update_id": 2, "message": {
                "message_id": 6, "text": "/start", "message_thread_id": 2,
                "is_topic_message": True,
                "chat": {"id": -1001234567890, "type": "supergroup",
                         "title": "Mail", "is_forum": True}}}]})
        result = self.bridge("--find-chat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("chat_id 31337  private Alex (@alex)", result.stdout)
        self.assertIn('chat_id -1001234567890  supergroup "Mail"  topics: '
                      "thread_id 2", result.stdout)
        self.assertIn("Use a chat of your own", result.stdout)
        self.assertNotIn("/start@", result.stdout)

    def test_find_chat_explains_how_a_topic_reaches_the_bot(self):
        self.write_config(telegram={"chat_id": ""})
        self.api.reply("getUpdates", 200, {"ok": True, "result": [
            {"update_id": 1, "my_chat_member": {
                "chat": {"id": -100555, "type": "supergroup", "title": "Mail",
                         "is_forum": True},
                "new_chat_member": {"status": "member"}}}]})
        result = self.bridge("--find-chat")
        self.assertIn('chat_id -100555  supergroup "Mail"', result.stdout)
        self.assertIn("send /start@mail_test_bot inside the topic",
                      result.stdout)

    def test_find_chat_without_messages_says_what_to_do(self):
        result = self.bridge("--find-chat")
        self.assertEqual(result.returncode, 1)
        self.assertIn("write anything to @mail_test_bot", result.stdout)
        self.assertIn("/start@mail_test_bot", result.stdout)

    def test_preview_from_a_terminal_without_access_does_not_blame_the_app(self):
        self.write_config(db_path=self.path("absent", "db"))
        result = self.bridge("--preview")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the bridge does not need it to", result.stdout)
        self.assertNotIn("kickstart", result.stdout)

    def test_preview_shows_the_latest_and_sends_nothing(self):
        for subject in ("One", "Two", "Three"):
            self.center.post(subject)
        result = self.bridge("--preview", "2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("One", result.stdout)
        self.assertLess(result.stdout.index("Two"),
                        result.stdout.index("Three"))
        self.assertIn("Nothing was sent.", result.stdout)
        self.assertEqual(self.texts(), [])
        self.assertFalse(os.path.exists(self.path("state", "state.json")))

    def test_a_config_problem_exits_with_2(self):
        self.write_config(poll_seconds=0)
        result = self.bridge("--once")
        self.assertEqual(result.returncode, 2)
        self.assertIn("poll_seconds", result.stderr)

    def test_the_daemon_forwards_and_stops_cleanly(self):
        daemon = self.start_daemon()
        self.assertTrue(wait_until(
            lambda: os.path.exists(self.path("state", "state.json"))))
        self.center.post("Live mail")
        self.assertTrue(wait_until(lambda: self.texts()), "nothing sent")
        second = self.bridge()
        self.assertEqual(second.returncode, 3)
        self.assertIn("running already", second.stdout)
        output = self.stop(daemon)
        self.assertEqual(daemon.returncode, 0, output)
        self.assertIn("stopped", output)
        self.assertEqual(len(self.texts()), 1)

    def test_the_daemon_reports_its_status_for_check(self):
        daemon = self.start_daemon()
        status_path = self.path("state", "status.json")
        self.assertTrue(wait_until(lambda: os.path.exists(status_path)))
        self.center.post("Live mail")
        self.assertTrue(wait_until(lambda: self.texts()), "nothing sent")

        def status():
            return otb.read_status(status_path) or {}
        self.assertTrue(wait_until(lambda: status().get("delivery") == "ok"))
        self.assertEqual(status()["pid"], daemon.pid)
        self.assertEqual((status()["database"], status()["outlook"]),
                         ("ok", True))
        self.stop(daemon)

    def test_the_daemon_reports_an_unreadable_database(self):
        self.write_config(db_path=self.path("absent", "db"))
        daemon = self.start_daemon()
        status_path = self.path("state", "status.json")
        self.assertTrue(wait_until(lambda: os.path.exists(status_path)))
        self.assertIn("unable to open", otb.read_status(status_path)["database"])
        output = self.stop(daemon)
        self.assertEqual(output.count("cannot read the notification database"),
                         1, output)

    @unittest.skipUnless(sys.platform == "darwin",
                         "orphans go to launchd, pid 1, on macOS")
    def test_an_orphaned_daemon_stops(self):
        for broken in (False, True):
            with self.subTest(waiting_for_config=broken):
                self.write_config(notify_content="loud" if broken else "full")
                log = self.path(f"orphan-{broken}.log")
                shell = subprocess.run(
                    ["/bin/sh", "-c", '"$0" "$1" >"$2" 2>&1 & echo $!',
                     sys.executable, support.SCRIPT, log],
                    env=dict(self.env, OTB_VIA_APP="1"), capture_output=True,
                    text=True, timeout=30)
                pid = int(shell.stdout)

                def gone():
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                    return False
                self.assertTrue(wait_until(gone), "the orphan kept running")
                with open(log) as f:
                    self.assertIn("is gone; stopping", f.read())

    def test_sigterm_ends_a_wait_for_telegram(self):
        self.api.reply("sendMessage", 429, support.error(
            429, "Too Many Requests", parameters={"retry_after": 60}))
        daemon = self.start_daemon()
        self.assertTrue(wait_until(
            lambda: os.path.exists(self.path("state", "state.json"))))
        self.center.post("Held by a 429")
        self.assertTrue(wait_until(lambda: self.api.sent()))
        time.sleep(0.3)
        started = time.monotonic()
        output = self.stop(daemon)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(daemon.returncode, 0, output)
        self.assertIn("stopped", output)

    def test_the_daemon_waits_out_a_broken_config(self):
        self.write_config(notify_content="loud")
        daemon = self.start_daemon()
        time.sleep(1)
        self.assertIsNone(daemon.poll(), "the daemon exited")
        output = self.stop(daemon)
        self.assertEqual(daemon.returncode, 0, output)
        self.assertIn("config problem", output)


if __name__ == "__main__":
    unittest.main()
