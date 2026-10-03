import contextlib
import io
import json
import os
import signal
import sqlite3
import unittest
from unittest import mock

import support
from support import otb

MIDDLE_DOT = " · "


class BridgeTest(support.TempDirTest):
    def setUp(self):
        super().setUp()
        self.center = support.NotificationCenter(
            self.path("Group Containers", "db"))
        self.addCleanup(self.center.close)
        self.state = self.path("state", "state.json")
        self.cfg = self.config(db_path=self.center.path)
        self.sent = []
        self.answers = []
        self.now = support.mac_now()

    def send(self, text, links=()):
        self.sent.append(text)
        return self.answers.pop(0) if self.answers else (True, "")

    def bridge(self, cfg=None, now=None):
        return otb.Bridge(cfg or self.cfg, self.state, self.send,
                          now=self.now if now is None else now)

    def poll(self, bridge):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = bridge.poll()
        self.log = out.getvalue()
        return result

    def subjects(self):
        return [text.split(MIDDLE_DOT)[1] for text in self.sent]

    def test_the_first_run_skips_what_came_before_it(self):
        old = self.center.post("Old", at=self.now - 100)
        self.center.post("Older", at=self.now - 200)
        self.assertTrue(self.poll(self.bridge()))
        self.assertEqual(self.sent, [])
        seen, last = otb.read_state(self.state)
        self.assertIn(old, seen)
        self.assertEqual((len(seen), last), (2, None))

    def test_mail_after_the_start_counts_even_before_access_works(self):
        # The bridge starts, Full Disk Access arrives later: mail delivered
        # in between is still new.
        self.center.post("While waiting for access", at=self.now + 30)
        self.poll(self.bridge())
        self.assertEqual(self.subjects(), ["While waiting for access"])

    def test_new_mail_is_forwarded_once(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("Report", "Ivan Petrov", "Numbers inside")
        self.poll(bridge)
        self.poll(bridge)
        self.assertEqual(self.sent, [
            "\U0001F4E7 Ivan Petrov" + MIDDLE_DOT + "Report" + MIDDLE_DOT
            + "Numbers inside"])

    def test_mail_goes_out_oldest_first(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("Second", at=self.now + 20)
        self.center.post("First", at=self.now + 10)
        self.poll(bridge)
        self.assertEqual(self.subjects(), ["First", "Second"])

    def test_notifications_with_the_same_date_all_go_out(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("One", at=self.now + 5)
        self.center.post("Two", at=self.now + 5)
        self.poll(bridge)
        self.poll(bridge)
        self.assertEqual(sorted(self.subjects()), ["One", "Two"])

    def test_a_date_in_the_future_holds_nothing_back(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("From a clock gone wrong", at=self.now + 86400)
        self.poll(bridge)
        self.center.post("Ordinary mail")
        self.poll(bridge)
        self.assertEqual(sorted(self.subjects()),
                         ["From a clock gone wrong", "Ordinary mail"])

    def test_a_notification_written_after_a_newer_one_still_goes_out(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("Newer", at=self.now + 20)
        self.poll(bridge)
        self.center.post("Older, written late", at=self.now + 10)
        self.poll(bridge)
        self.assertEqual(self.subjects(), ["Newer", "Older, written late"])

    def test_other_apps_are_ignored(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("Not Outlook", app=support.NotificationCenter.OTHER)
        self.poll(bridge)
        self.assertEqual(self.sent, [])

    def test_mail_counts_as_new_when_outlook_registers_late(self):
        self.center = support.NotificationCenter(
            self.path("fresh", "db"), outlook=False)
        self.addCleanup(self.center.close)
        bridge = self.bridge(self.config(db_path=self.center.path))
        self.assertTrue(self.poll(bridge))
        self.assertIn("waiting", self.log)
        self.center.register_outlook()
        self.center.post("The very first mail")
        self.poll(bridge)
        self.assertEqual(self.subjects(), ["The very first mail"])

    def test_a_record_without_text_is_skipped(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post(title=None, sender=None, body=None)
        self.center.post("Readable")
        self.assertTrue(self.poll(bridge))
        self.assertEqual(self.subjects(), ["Readable"])
        self.assertIn("without text", self.log)
        self.poll(bridge)
        self.assertEqual(len(self.sent), 1)

    def test_an_emoji_cut_in_half_is_repaired_not_lost(self):
        bridge = self.bridge()
        self.poll(bridge)
        pair = "\U0001F600".encode("utf-16-be")
        blob = support.payload("Subject", "Sender", "Hi \U0001F600")
        self.center.post(data=blob.replace(pair, pair[:2]
                                           + "!".encode("utf-16-be")))
        self.poll(bridge)
        self.assertEqual(self.sent, ["\U0001F4E7 Sender" + MIDDLE_DOT
                                     + "Subject" + MIDDLE_DOT + "Hi �!"])

    def test_an_unreadable_record_waits_then_goes_out_as_a_placeholder(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post(data=b"not a plist")
        self.center.post("Behind it")
        clock = [1000.0]
        with mock.patch.object(otb.time, "monotonic", lambda: clock[0]):
            self.assertTrue(self.poll(bridge))
            self.assertEqual(self.sent, [])
            clock[0] += otb.UNREADABLE_GRACE
            self.poll(bridge)
        self.assertEqual(self.sent[0],
                         "\U0001F4E7 Outlook notification the bridge could "
                         "not read")
        self.assertEqual(self.sent[1].split(MIDDLE_DOT)[1], "Behind it")
        self.assertIn("placeholder", self.log)

    def test_a_failed_delivery_is_retried_and_keeps_the_order(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("A", at=self.now + 1)
        self.center.post("B", at=self.now + 2)
        self.answers = [(False, "HTTP 502")]
        self.assertFalse(self.poll(bridge))
        self.assertIn("HTTP 502", self.log)
        self.assertEqual(bridge.delivery, "HTTP 502")
        self.assertEqual(otb.read_state(self.state)[1], None)
        self.assertTrue(self.poll(bridge))
        self.assertEqual(self.subjects(), ["A", "A", "B"])
        self.assertEqual(bridge.delivery, "ok")

    def test_a_restart_resumes_without_repeats(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("Before the restart")
        self.poll(bridge)
        self.center.post("While stopped")
        self.poll(self.bridge(now=self.now + 3600))
        self.assertEqual(self.subjects(),
                         ["Before the restart", "While stopped"])

    def test_a_damaged_state_file_means_a_fresh_start(self):
        self.center.post("Old", at=self.now - 100)
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as f:
            f.write("{broken")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            bridge = self.bridge()
        self.assertIn("damaged or unreadable", out.getvalue())
        self.poll(bridge)
        self.assertEqual(self.sent, [])

    def remove(self, key):
        row = self.center.con.execute(
            "SELECT * FROM record WHERE hex(uuid) = ?", (key.upper(),)).fetchone()
        self.center.con.execute("DELETE FROM record WHERE hex(uuid) = ?",
                                (key.upper(),))
        self.center.con.commit()
        return row

    def restore(self, row):
        self.center.con.execute(
            f"INSERT INTO record VALUES ({','.join('?' * len(row))})", row)
        self.center.con.commit()

    def test_seen_forgets_what_macos_deleted_a_day_ago(self):
        bridge = self.bridge()
        self.poll(bridge)
        kept = self.center.post("Kept")
        gone = self.center.post("Dismissed")
        self.poll(bridge)
        self.remove(gone)
        self.poll(bridge)
        self.assertEqual(otb.read_state(self.state)[0], {kept, gone})
        with mock.patch.object(otb, "FORGET_AFTER", 0):
            self.poll(bridge)
        self.assertEqual(otb.read_state(self.state)[0], {kept})
        self.assertEqual(len(self.sent), 2)

    def test_a_notification_gone_for_a_moment_is_not_sent_again(self):
        bridge = self.bridge()
        self.poll(bridge)
        key = self.center.post("Flickers")
        self.poll(bridge)
        row = self.remove(key)
        self.poll(bridge)
        self.restore(row)
        self.poll(bridge)
        self.assertEqual(self.subjects(), ["Flickers"])

    def test_the_same_uuid_twice_goes_out_once(self):
        bridge = self.bridge()
        self.poll(bridge)
        row = self.remove(self.center.post("Twice"))
        self.restore(row)
        self.restore((None,) + tuple(row[1:]))
        self.poll(bridge)
        self.assertEqual(self.subjects(), ["Twice"])

    def test_a_failure_whose_mail_vanished_stops_being_reported(self):
        bridge = self.bridge()
        self.poll(bridge)
        key = self.center.post("Read on the Mac before Telegram was back")
        self.answers = [(False, "HTTP 502")]
        self.poll(bridge)
        self.assertEqual(bridge.delivery, "HTTP 502")
        self.remove(key)
        self.poll(bridge)
        self.assertIsNone(bridge.delivery)

    def test_unreadable_records_wait_together_not_in_turn(self):
        bridge = self.bridge()
        self.poll(bridge)
        for _ in range(3):
            self.center.post(data=b"not a plist")
        self.center.post("Readable")
        clock = [1000.0]
        with mock.patch.object(otb.time, "monotonic", lambda: clock[0]):
            self.poll(bridge)
            clock[0] += otb.UNREADABLE_GRACE
            self.poll(bridge)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(self.sent[3].split(MIDDLE_DOT)[1], "Readable")

    def test_a_log_that_fails_does_not_repeat_the_mail(self):
        class BrokenPipe(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("Logged to a closed pipe")
        with mock.patch("sys.stdout", BrokenPipe()):
            bridge.poll()
            bridge.poll()
        self.assertEqual(self.subjects(), ["Logged to a closed pipe"])

    def test_jira_mail_gets_its_links(self):
        handed = []
        cfg = self.config(db_path=self.center.path,
                          jira={"base_url": "https://jira.example.com"})
        bridge = otb.Bridge(cfg, self.state,
                            lambda text, links: handed.append(links)
                            or (True, ""), now=self.now)
        self.poll(bridge)
        self.center.post("[JIRA] Updates for PAY-7: limits", "Jira", "PAY-7")
        self.center.post("Lunch?", "Alex", "PAY-7 aside")
        self.poll(bridge)
        self.assertEqual([len(links) for links in handed], [2, 0])

    def test_a_bug_in_the_links_costs_the_links_not_the_mail(self):
        bridge = self.bridge(self.config(
            db_path=self.center.path,
            jira={"base_url": "https://jira.example.com"}))
        self.poll(bridge)
        self.center.post("[JIRA] Updates for PAY-7: limits")
        with mock.patch.object(otb, "issue_links", side_effect=KeyError("x")):
            self.poll(bridge)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Jira links skipped", self.log)

    def test_the_log_holds_no_mail_content_unless_asked(self):
        bridge = self.bridge()
        self.poll(bridge)
        self.center.post("Secret subject", "Secret sender", "Secret body")
        self.poll(bridge)
        self.assertIn("forwarded mail", self.log)
        self.assertNotIn("Secret", self.log)
        bridge = self.bridge(self.config(db_path=self.center.path,
                                         log_content=True))
        self.center.post("Visible subject")
        self.poll(bridge)
        self.assertIn("Visible subject", self.log)

    def test_an_unreadable_database_raises(self):
        cfg = self.config(db_path=self.path("absent", "db"))
        with self.assertRaises(sqlite3.Error):
            self.poll(self.bridge(cfg))


class StateTest(support.TempDirTest):
    def test_round_trip(self):
        path = self.path("deep", "state.json")
        otb.write_state(path, {"b", "a"}, 123.5)
        self.assertEqual(otb.read_state(path), ({"a", "b"}, 123.5))
        otb.write_state(path, set(), None)
        self.assertEqual(otb.read_state(path), (set(), None))
        self.assertEqual(os.listdir(self.path("deep")), ["state.json"])
        with open(path) as f:
            self.assertEqual(json.load(f), {"seen": [], "last": None})

    def test_missing_or_damaged_state_reads_as_none(self):
        path = self.path("state.json")
        self.assertEqual(otb.read_state(path), (None, None))
        for content in ("x", "[]", "null", '{"seen": "abc"}', '{"last": 1}',
                        '{"seen": [], "last": "soon"}', '{"seen": 5}'):
            with self.subTest(content=content):
                with open(path, "w") as f:
                    f.write(content)
                self.assertEqual(otb.read_state(path), (None, None))


class RepairTest(unittest.TestCase):
    def test_only_strings_change_and_only_their_broken_halves(self):
        blob = support.payload("Тема \U0001F600", "Отправитель", "Текст")
        self.assertEqual(otb.repair_utf16(blob), blob)
        pair = "\U0001F600".encode("utf-16-be")
        broken = blob.replace(pair, pair[2:] + pair[2:])
        self.assertEqual(otb.parse_record(broken).title,
                         "Тема ��")

    def test_what_it_cannot_walk_comes_back_as_it_was(self):
        for blob in (b"", b"not a plist", b"bplist00\xff", None):
            with self.subTest(blob=blob):
                self.assertIs(otb.repair_utf16(blob), blob)

    def test_a_broken_trailer_is_left_alone_at_once(self):
        class Hung(BaseException):
            pass

        def interrupt(*args):
            raise Hung()

        blob = support.payload("Тема", "Кто", "Текст")
        samples = [blob[:cut] for cut in range(9, len(blob))]
        samples += [blob + b"\0" * 7, blob + b"junk",
                    b"bplist00" + b"\xff" * 40]
        previous = signal.signal(signal.SIGALRM, interrupt)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        for sample in samples:
            signal.setitimer(signal.ITIMER_REAL, 1)
            try:
                self.assertIs(otb.repair_utf16(sample), sample)
            except Hung:
                self.fail(f"repair_utf16 hangs on {len(sample)} bytes")
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
            with self.assertRaises(ValueError):
                otb.parse_record(sample)


if __name__ == "__main__":
    unittest.main()
