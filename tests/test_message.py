import plistlib
import unittest

import support
from support import otb

MAIL_ICON = "\U0001F4E7"
CALENDAR_ICON = "\U0001F4C5"


class ParseTest(unittest.TestCase):
    def test_reads_what_outlook_stores(self):
        note = otb.parse_record(support.payload("Subject", "Sender", "Body"))
        self.assertEqual(note, otb.Notification("Subject", "Sender", "Body",
                                                support.MAIL))

    def test_unreadable_records_raise_value_error(self):
        for blob in (b"not a plist", b"", None, plistlib.dumps([1, 2]),
                     plistlib.dumps({"req": "text"}),
                     plistlib.dumps({"other": {}})):
            with self.subTest(blob=blob):
                with self.assertRaises(ValueError):
                    otb.parse_record(blob)

    def test_fields_that_are_not_text_count_as_missing(self):
        blob = plistlib.dumps({"req": {"titl": 5, "subt": b"x",
                                       "body": "ok", "cate": ["c"]}})
        self.assertEqual(otb.parse_record(blob),
                         otb.Notification(None, None, "ok", None))


class FormatTest(support.TempDirTest):
    def text(self, title, sender, body, category=support.MAIL, **settings):
        return otb.format_message(self.config(**settings), otb.Notification(
            title, sender, body, category))

    def test_full_leads_with_the_sender(self):
        # Outlook stores the subject in titl and the sender in subt.
        self.assertEqual(self.text("Subject", "Sender", "Preview"),
                         f"{MAIL_ICON} Sender · Subject · Preview")

    def test_sender_only_hides_the_subject_and_preview(self):
        self.assertEqual(self.text("Subject", "Sender", "Preview",
                                   notify_content="sender_only"),
                         f"{MAIL_ICON} Sender")

    def test_sender_only_without_a_sender_still_pings(self):
        self.assertEqual(self.text("Subject", None, "Preview",
                                   notify_content="sender_only"),
                         f"{MAIL_ICON} New mail in Outlook")

    def test_minimal_sends_no_content(self):
        text = self.text("Secret subject", "Secret sender", "Secret body",
                         notify_content="minimal")
        self.assertEqual(text, f"{MAIL_ICON} New mail in Outlook")
        self.assertNotIn("Secret", text)

    def test_reminders_have_their_own_prefix_and_placeholder(self):
        self.assertEqual(self.text("Standup", "10:00", "", support.REMINDER),
                         f"{CALENDAR_ICON} 10:00 · Standup")
        self.assertEqual(self.text("Standup", "10:00", "", support.REMINDER,
                                   notify_content="minimal"),
                         f"{CALENDAR_ICON} Reminder in Outlook")

    def test_a_record_without_text_is_skipped_in_every_mode(self):
        for level in otb.CONTENT_LEVELS:
            for fields in ((None, None, None), ("", " ", "\n")):
                with self.subTest(level=level, fields=fields):
                    self.assertIsNone(self.text(*fields,
                                                notify_content=level))

    def test_blank_parts_are_dropped_and_the_rest_trimmed(self):
        self.assertEqual(self.text("  Subject ", "", " Body\n"),
                         f"{MAIL_ICON} Subject · Body")

    def test_prefixes_and_placeholders_are_settings(self):
        settings = {"message_prefix": "[mail] ", "empty_message": "mail!",
                    "calendar_prefix": "[cal] ", "empty_reminder": "meeting!",
                    "notify_content": "minimal"}
        self.assertEqual(self.text("S", "F", "B", **settings), "[mail] mail!")
        self.assertEqual(self.text("S", "F", "B", support.REMINDER,
                                   **settings), "[cal] meeting!")

    def test_long_text_is_cut_to_the_telegram_limit(self):
        for body in ("x" * 5000, "\U0001F600" * 3000):
            with self.subTest(body=body[:3]):
                text = self.text("Subject", "Sender", body)
                self.assertLessEqual(otb.utf16_len(text), otb.TEXT_LIMIT)
                self.assertTrue(text.endswith("…"))
        short = self.text("Subject", "Sender", "x" * 100)
        self.assertFalse(short.endswith("…"))

    def test_is_calendar_takes_any_value(self):
        self.assertTrue(otb.is_calendar(support.REMINDER))
        for category in (support.MAIL, None, 123, ""):
            self.assertFalse(otb.is_calendar(category))


class IssueLinkTest(support.TempDirTest):
    SUBJECT = "[JIRA] Updates for PAY-1020: Card limits"

    def setUp(self):
        super().setUp()
        self.cfg = self.config(jira={"base_url": "https://jira.example.com/"})
        self.text = otb.format_message(self.cfg, otb.Notification(
            self.SUBJECT, "Maria Lopez \U0001F680 (Jira)",
            "Payments / PAY-1020 In progress", support.MAIL))

    def linked(self, title, text, cfg=None):
        return [text[start:end] for start, end, _ in
                otb.issue_links(cfg or self.cfg, title, text)]

    def test_the_subject_issue_is_linked_wherever_it_appears(self):
        links = otb.issue_links(self.cfg, self.SUBJECT, self.text)
        self.assertEqual([self.text[s:e] for s, e, _ in links],
                         ["PAY-1020", "PAY-1020"])
        self.assertEqual({url for _, _, url in links},
                         {"https://jira.example.com/browse/PAY-1020"})

    def test_other_key_shaped_words_stay_plain(self):
        text = self.text + " see OPS-13983, ISO-8583, cut off PAY-10"
        self.assertEqual(self.linked(self.SUBJECT, text),
                         ["PAY-1020", "PAY-1020"])

    def test_mail_from_another_jira_gets_no_links(self):
        self.assertEqual(self.linked("Jira Bank: PAY-1020 updated",
                                     "Jira Bank: PAY-1020 updated"), [])

    def test_a_subject_without_an_issue_gets_no_links(self):
        self.assertEqual(self.linked("[JIRA] Tempo reminder",
                                     "[JIRA] Tempo reminder PAY-1020"), [])

    def test_a_key_inside_a_link_or_path_is_left_alone(self):
        text = (self.SUBJECT + " https://jira.other.example/browse/PAY-1020"
                " jira.other.example:8443/browse/PAY-1020")
        self.assertEqual(self.linked(self.SUBJECT, text), ["PAY-1020"])

    def test_no_base_url_no_links(self):
        self.assertEqual(self.linked(self.SUBJECT, self.text,
                                     self.config()), [])

    def test_jira_default_prefix_is_required_by_default(self):
        cfg = self.config(jira={"base_url": "https://j.example.com"})
        self.assertEqual(self.linked("[JIRA] Updates for PAY-1: x",
                                     "[JIRA] Updates for PAY-1: x", cfg),
                         ["PAY-1"])
        self.assertEqual(self.linked("Re: PAY-1", "Re: PAY-1", cfg), [])

    def test_an_empty_prefix_accepts_any_mail(self):
        cfg = self.config(jira={"base_url": "https://j.example.com",
                                "subject_prefix": ""})
        self.assertEqual(self.linked("Re: PAY-1", "Re: PAY-1", cfg),
                         ["PAY-1"])

    def test_entity_offsets_count_utf16_units(self):
        links = otb.issue_links(self.cfg, self.SUBJECT, self.text)
        units = self.text.encode("utf-16-le")
        found = [units[2 * e["offset"]:2 * (e["offset"] + e["length"])]
                 .decode("utf-16-le")
                 for e in otb.telegram_entities(self.text, links)]
        self.assertEqual(found, ["PAY-1020", "PAY-1020"])


if __name__ == "__main__":
    unittest.main()
