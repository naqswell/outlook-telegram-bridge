import copy
import json
import os
import unittest

import support
from support import otb


class ConfigTest(support.TempDirTest):
    def load(self, given, need_chat=True, token=support.TOKEN):
        path = self.path("config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(given if isinstance(given, str) else json.dumps(given))
        if token is not None:
            otb.write_secret(otb.token_path(path), token)
        return otb.load_config(path, need_chat)

    def problem(self, need_chat=True, token=support.TOKEN, **settings):
        """The ConfigError text for settings over a valid config, or None."""
        try:
            otb.load_config(support.write_config(
                self.path("config.json"), token, **settings), need_chat)
        except otb.ConfigError as e:
            return str(e)
        return None

    def test_a_token_and_a_chat_are_enough(self):
        cfg = self.load({"telegram": {"chat_id": "42"}})
        self.assertEqual(cfg["telegram"]["bot_token"], support.TOKEN)
        self.assertEqual(cfg["notify_content"], "full")
        self.assertEqual(cfg["defocus_outlook"],
                         {"enabled": True, "idle_seconds": 180,
                          "activate": "Finder"})
        self.assertEqual(cfg["telegram"]["api_url"],
                         "https://api.telegram.org")
        self.assertIsNone(cfg["telegram"]["thread_id"])
        self.assertTrue(cfg["db_path"].endswith("/db"), cfg["db_path"])

    def test_a_partial_section_keeps_its_other_defaults(self):
        cfg = self.config(defocus_outlook={"idle_seconds": 60})
        self.assertEqual(cfg["defocus_outlook"],
                         {"enabled": False, "idle_seconds": 60,
                          "activate": "Finder"})

    def test_loading_leaves_the_defaults_alone(self):
        before = copy.deepcopy(otb.DEFAULTS)
        cfg = self.load({"telegram": {"chat_id": "42"}})
        cfg["jira"]["base_url"] = "https://changed.example.com"
        cfg["defocus_outlook"]["enabled"] = False
        self.assertEqual(otb.DEFAULTS, before)

    def test_missing_file_says_how_to_get_one(self):
        with self.assertRaisesRegex(otb.ConfigError, "install.sh"):
            otb.load_config(self.path("absent.json"))

    def test_broken_json_says_where(self):
        with self.assertRaisesRegex(otb.ConfigError, "not valid JSON.*line 1"):
            self.load('{"telegram": ')

    def test_the_top_level_must_be_an_object(self):
        with self.assertRaisesRegex(otb.ConfigError, "JSON object"):
            self.load("[]")

    def test_a_misspelled_setting_is_named(self):
        self.assertIn("notify_contnet", self.problem(notify_contnet="minimal"))
        self.assertIn("telegram.chat", self.problem(telegram={"chat": "42"}))
        self.assertIn("jira.url", self.problem(jira={"url": "https://j.io"}))

    def test_underscore_keys_are_comments(self):
        self.assertIsNone(self.problem(_help="see README",
                                       telegram={"_note": "my bot"}))

    def test_spaces_around_the_token_and_chat_are_dropped(self):
        cfg = self.load({"telegram": {"chat_id": " 42 "}},
                        token=f"  {support.TOKEN}\n")
        self.assertEqual(cfg["telegram"]["bot_token"], support.TOKEN)
        self.assertEqual(cfg["telegram"]["chat_id"], "42")

    def test_the_token_comes_from_its_own_file(self):
        self.assertIn("--set-token", self.problem(token=None))
        for token in ("", "bot123456789:AAE_test-token-0123456789",
                      "123456789", f"https://api.telegram.org/bot{support.TOKEN}"):
            with self.subTest(token=token):
                self.assertIn("--set-token", self.problem(token=token))

    def test_a_token_in_the_config_is_turned_away(self):
        found = self.problem(telegram={"bot_token": support.TOKEN})
        self.assertIn("does not belong", found)
        self.assertIn("--set-token", found)

    def test_chat_id_forms(self):
        for chat in (42, "42", -1001234567890, "-1001234567890",
                     "@mail_channel"):
            with self.subTest(chat=chat):
                self.assertIsNone(self.problem(telegram={"chat_id": chat}))
        for chat in ("abc", "42 43", True, 1.5, [42], "@ab"):
            with self.subTest(chat=chat):
                self.assertIn("chat_id",
                              self.problem(telegram={"chat_id": chat}))

    def test_an_empty_chat_waits_for_find_chat(self):
        self.assertIn("--find-chat", self.problem(telegram={"chat_id": ""}))
        self.assertIsNone(self.problem(need_chat=False,
                                       telegram={"chat_id": ""}))

    def test_thread_id(self):
        for thread, loaded in ((None, None), ("", None), (5, 5), ("5", 5)):
            with self.subTest(thread=thread):
                cfg = self.config(telegram={"thread_id": thread})
                self.assertEqual(cfg["telegram"]["thread_id"], loaded)
        for thread in (0, -3, "0", "-5", "abc", "５", True, 1.5):
            with self.subTest(thread=thread):
                self.assertIn("thread_id",
                              self.problem(telegram={"thread_id": thread}))

    def test_proxy(self):
        for proxy in ("", "http://127.0.0.1:8080",
                      "https://proxy.corp.example:3128/"):
            with self.subTest(proxy=proxy):
                self.assertIsNone(self.problem(telegram={"proxy": proxy}))
        for proxy in ("127.0.0.1:8080", "socks5://127.0.0.1:1080", 8080,
                      "http://proxy.example:3128/path"):
            with self.subTest(proxy=proxy):
                self.assertIn("telegram.proxy",
                              self.problem(telegram={"proxy": proxy}))

    def test_values_are_checked(self):
        for settings, fragment in (
                ({"notify_content": "everything"}, "notify_content"),
                ({"poll_seconds": 0}, "poll_seconds"),
                ({"poll_seconds": "5"}, "poll_seconds"),
                ({"poll_seconds": True}, "poll_seconds"),
                ({"defocus_outlook": {"enabled": "yes"}},
                 "defocus_outlook.enabled"),
                ({"defocus_outlook": {"idle_seconds": 5}},
                 "defocus_outlook.idle_seconds"),
                ({"defocus_outlook": {"activate": " "}},
                 "defocus_outlook.activate"),
                ({"defocus_outlook": False}, "defocus_outlook must be"),
                ({"jira": {"base_url": "jira.example.com"}}, "jira.base_url"),
                ({"jira": {"subject_prefix": ["A", "B"]}}, "jira must be"),
                ({"jira": "https://jira.example.com"}, "jira must be"),
                ({"jira": None}, "jira must be"),
                ({"message_prefix": None}, "message_prefix"),
                ({"empty_reminder": 1}, "empty_reminder"),
                ({"log_content": "yes"}, "log_content"),
                ({"outlook_bundle_id": ""}, "outlook_bundle_id"),
                ({"telegram": None}, "telegram must be"),
                ({"telegram": {"api_url": "api.telegram.org"}},
                 "telegram.api_url")):
            with self.subTest(settings=settings):
                self.assertIn(fragment, self.problem(**settings) or "")

    def test_half_an_emoji_in_a_prefix_is_a_problem(self):
        path = self.path("config.json")
        support.write_config(path)
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw[:-1] + ', "message_prefix": "\\ud83d "}')
        with self.assertRaisesRegex(otb.ConfigError, "broken character"):
            otb.load_config(path)

    def test_every_problem_is_reported_at_once(self):
        found = self.problem(notify_content="all", poll_seconds=-1,
                             token=None)
        for fragment in ("notify_content", "poll_seconds", "bot token"):
            self.assertIn(fragment, found)

    def test_db_path(self):
        self.assertEqual(self.config(db_path="~/db")["db_path"],
                         os.path.expanduser("~/db"))
        self.assertEqual(self.config()["db_path"], otb.default_db_path())

    def test_the_example_config_is_valid_once_filled_in(self):
        with open(os.path.join(support.ROOT, "config.example.json"),
                  encoding="utf-8") as f:
            example = json.load(f)
        with self.assertRaises(otb.ConfigError) as caught:
            self.load(example, token=None)
        self.assertIn("no bot token", str(caught.exception))
        self.assertIn("chat_id", str(caught.exception))
        example["telegram"].update(chat_id="42")
        cfg = self.load(example)
        self.assertEqual(cfg["notify_content"], "full")


if __name__ == "__main__":
    unittest.main()
