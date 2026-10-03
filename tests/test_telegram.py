import contextlib
import io
import json
import os
import socket
import unittest
import urllib.parse
from unittest import mock

import support
from support import otb, error

TEXT = "\U0001F4E7 Alex Smith · Quarterly report"


class TelegramTest(support.TempDirTest):
    def setUp(self):
        super().setUp()
        self.api = support.FakeTelegram()
        self.addCleanup(self.api.close)
        self.slept = []

    def client(self, **telegram):
        cfg = self.config(telegram=dict({"api_url": self.api.url}, **telegram))
        return otb.Telegram(cfg["telegram"], sleep=self.slept.append)

    def send(self, text=TEXT, links=(), **telegram):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = self.client(**telegram).send(text, links)
        self.log = out.getvalue()
        return result

    def too_many(self, seconds=None):
        extra = {"parameters": {"retry_after": seconds}} if seconds else {}
        return 429, error(429, "Too Many Requests", **extra)

    def test_a_plain_message(self):
        self.assertEqual(self.send(), (True, ""))
        [call] = self.api.calls_of("sendMessage")
        self.assertEqual(call["token"], support.TOKEN)
        self.assertEqual(call["fields"], {
            "chat_id": "42", "text": TEXT,
            "link_preview_options": json.dumps({"is_disabled": True})})

    def test_links_become_text_link_entities(self):
        text = "\U0001F4E7 Jira · [JIRA] PAY-7: limits"
        start = text.index("PAY-7")
        self.send(text, [(start, start + 5, "https://j.example.com/browse/PAY-7")])
        [fields] = self.api.sent()
        self.assertEqual(json.loads(fields["entities"]), [
            {"type": "text_link", "offset": start + 1, "length": 5,
             "url": "https://j.example.com/browse/PAY-7"}])

    def test_a_topic_goes_out_as_message_thread_id(self):
        self.send(thread_id=5, chat_id="-1001234567890")
        [fields] = self.api.sent()
        self.assertEqual(fields["message_thread_id"], "5")
        self.assertEqual(fields["chat_id"], "-1001234567890")

    def test_ok_false_is_a_failure(self):
        self.api.reply("sendMessage", 200, {"ok": False,
                                            "description": "odd"})
        ok, err = self.send()
        self.assertFalse(ok)
        self.assertIn("odd", err)

    def test_an_answer_that_is_not_json_is_a_failure(self):
        self.api.reply("sendMessage", 200, b"<html>proxy login</html>")
        ok, err = self.send()
        self.assertFalse(ok)
        self.assertIn("network login page", err)
        self.assertNotIn("<html>", err)

    def test_a_portal_page_quoting_the_url_does_not_leak_the_token(self):
        quoted = urllib.parse.quote(support.TOKEN, safe="")
        for status in (200, 403, 502):
            with self.subTest(status=status):
                self.api.reply("sendMessage", status, (
                    "<html>Blocked: https://api.telegram.org/bot"
                    f"{quoted}%2FsendMessage {support.TOKEN}</html>").encode())
                ok, err = self.send()
                self.assertFalse(ok)
                for form in (support.TOKEN, quoted,
                             urllib.parse.quote_plus(support.TOKEN)):
                    self.assertNotIn(form, err)

    def test_redact_covers_every_spelling_of_the_token(self):
        client = self.client()
        text = " ".join([support.TOKEN,
                         urllib.parse.quote(support.TOKEN, safe=""),
                         urllib.parse.quote_plus(support.TOKEN)])
        self.assertEqual(client.redact(text),
                         "<bot token> <bot token> <bot token>")

    def test_429_waits_as_asked_and_sends_the_same_message_again(self):
        self.api.reply("sendMessage", *self.too_many(7))
        self.assertEqual(self.send(), (True, ""))
        self.assertEqual(self.slept, [7])
        first, second = self.api.sent()
        self.assertEqual(first, second)

    def test_429_gives_up_after_three_waits_of_at_most_a_minute(self):
        for _ in range(4):
            self.api.reply("sendMessage", *self.too_many(999))
        ok, err = self.send()
        self.assertFalse(ok)
        self.assertIn("HTTP 429", err)
        self.assertEqual(self.slept, [60, 60, 60])
        self.assertEqual(len(self.api.sent()), 4)

    def test_429_without_retry_after_waits_five_seconds(self):
        self.api.reply("sendMessage", *self.too_many())
        self.assertTrue(self.send()[0])
        self.assertEqual(self.slept, [5])

    def test_400_costs_the_links_not_the_message(self):
        self.api.reply("sendMessage", 400,
                       error(400, "Bad Request: can't parse entities"))
        ok, _ = self.send(TEXT, [(2, 6, "https://j.example.com/browse/X-1")])
        self.assertTrue(ok)
        first, second = self.api.sent()
        self.assertIn("entities", first)
        self.assertNotIn("entities", second)

    def test_400_on_a_topic_falls_back_to_the_main_chat(self):
        self.api.reply("sendMessage", 400,
                       error(400, "Bad Request: message thread not found"))
        self.assertTrue(self.send(thread_id=5, chat_id="-100777")[0])
        first, second = self.api.sent()
        self.assertEqual(first["message_thread_id"], "5")
        self.assertNotIn("message_thread_id", second)
        self.assertIn("main chat", self.log)

    def test_without_fallback_a_refused_topic_is_a_failure(self):
        self.api.reply("sendMessage", 400,
                       error(400, "Bad Request: message thread not found"))
        client = self.client(thread_id=5, chat_id="-100777")
        with contextlib.redirect_stdout(io.StringIO()):
            ok, err = client.send(TEXT, fallback=False)
        self.assertFalse(ok)
        self.assertIn("telegram.thread_id", err)
        self.assertEqual(len(self.api.sent()), 1)

    def test_400_drops_the_links_first_and_then_the_topic(self):
        for _ in range(2):
            self.api.reply("sendMessage", 400, error(400, "Bad Request"))
        ok, _ = self.send(TEXT, [(2, 6, "https://j.example.com/browse/X-1")],
                          thread_id=5, chat_id="-100777")
        self.assertTrue(ok)
        calls = self.api.sent()
        self.assertEqual([("entities" in c, "message_thread_id" in c)
                          for c in calls],
                         [(True, True), (False, True), (False, False)])

    def test_400_in_the_main_chat_is_a_failure_not_a_loop(self):
        for _ in range(3):
            self.api.reply("sendMessage", 400, error(400, "Bad Request"))
        ok, err = self.send(thread_id=5, chat_id="-100777")
        self.assertFalse(ok)
        self.assertEqual(len(self.api.sent()), 2)
        self.assertIn("HTTP 400", err)

    def test_a_server_error_fails_at_once_for_the_next_poll(self):
        self.api.reply("sendMessage", 502, error(502, "Bad Gateway"))
        ok, err = self.send(TEXT, [(2, 6, "https://j.example.com/browse/X-1")],
                            thread_id=5, chat_id="-100777")
        self.assertFalse(ok)
        self.assertEqual(len(self.api.sent()), 1)
        self.assertIn("HTTP 502", err)

    def test_failures_explain_the_fix(self):
        for status, description, fragment in (
                (401, "Unauthorized", "@BotFather"),
                (404, "Not Found", "@BotFather"),
                (403, "Forbidden: bot was blocked by the user", "unblock"),
                (400, "Bad Request: chat not found", "write to the bot"),
                (409, "Conflict: terminated by other getUpdates request",
                 "bot of its own")):
            with self.subTest(status=status):
                self.api.reply("sendMessage", status,
                               error(status, description))
                ok, err = self.send()
                self.assertFalse(ok)
                self.assertIn(fragment, err)

    def test_no_answer_is_a_failure_that_keeps_the_token_secret(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ok, err = self.send(api_url=f"http://127.0.0.1:{port}")
        self.assertFalse(ok)
        self.assertIn("no answer from Telegram", err)
        self.assertIn("telegram.proxy", err)
        self.assertNotIn(support.TOKEN, err)

    def test_the_token_never_shows_in_an_error(self):
        # urllib quotes the whole URL, token included, when it lacks a scheme.
        tg = dict(self.config()["telegram"], api_url="api.example")
        ok, err = otb.Telegram(tg).send(TEXT)
        self.assertFalse(ok)
        self.assertIn("<bot token>", err)
        self.assertNotIn(support.TOKEN, err)

    def test_the_proxy_setting_is_used(self):
        # no_proxy=* from support would make urllib bypass any proxy.
        with mock.patch.dict(os.environ):
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            self.send(api_url="http://api.telegram.invalid",
                      proxy=self.api.url)
        [call] = self.api.calls_of("sendMessage")
        self.assertTrue(call["proxied"])

    def test_the_terminal_proxy_variables_are_ignored(self):
        # The background agent never gets them; a terminal that used them
        # would pass checks the agent then fails.
        dead = "http://127.0.0.1:9"
        with mock.patch.dict(os.environ, {"https_proxy": dead,
                                          "http_proxy": dead,
                                          "HTTPS_PROXY": dead}):
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            self.assertEqual(self.send(), (True, ""))
        self.assertFalse(self.api.calls_of("sendMessage")[0]["proxied"])

    def test_call_returns_the_result(self):
        reply = self.client().call("getMe")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.result["username"], "mail_test_bot")
        bad = otb.Telegram(dict(self.config()["telegram"],
                                api_url=self.api.url,
                                bot_token="999:AAE_wrong-token-0123456789abc"))
        reply = bad.call("getMe")
        self.assertFalse(reply.ok)
        self.assertIsNone(reply.result)
        self.assertIn("@BotFather", reply.error)


class RetryAfterTest(unittest.TestCase):
    def test_bounds_and_fallback(self):
        for payload, seconds in (
                ({"parameters": {"retry_after": 7}}, 7),
                ({"parameters": {"retry_after": 0}}, 1),
                ({"parameters": {"retry_after": 3600}}, 60),
                ({"parameters": {"retry_after": "x"}}, 5),
                ({"parameters": None}, 5), ({}, 5), (None, 5)):
            with self.subTest(payload=payload):
                self.assertEqual(otb.retry_after(payload), seconds)


if __name__ == "__main__":
    unittest.main()
