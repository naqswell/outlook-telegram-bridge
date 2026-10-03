"""Test fixtures: a Notification Center database shaped like the one on
macOS 26, and a stand-in for the Telegram Bot API on 127.0.0.1."""
import http.server
import json
import os
import plistlib
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "src", "outlook_telegram_bridge.py")
sys.path.insert(0, os.path.join(ROOT, "src"))

import outlook_telegram_bridge as otb  # noqa: E402

# Calls to the local stand-in must not go through a proxy from the
# environment or from System Settings.
os.environ["no_proxy"] = "*"

TOKEN = "123456789:AAE_test-token-0123456789abcdefghijk"
# Category names as Outlook sends them, typo included.
MAIL = "OLAlertsNotificationCategorydentifier"
REMINDER = "OLAlertsNotificationRemindersWithJoinCategoryIdentifier"

SCHEMA = """
CREATE TABLE dbinfo (key VARCHAR, value VARCHAR);
CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR,
                  badge INTEGER NULL);
CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB,
                     data BLOB, request_date REAL, request_last_date REAL,
                     delivered_date REAL, presented Bool, style INTEGER,
                     snooze_fire_date REAL);
CREATE TABLE requests (app_id INTEGER PRIMARY KEY, list BLOB);
CREATE TABLE delivered (app_id INTEGER PRIMARY KEY, list BLOB);
CREATE TABLE displayed (app_id INTEGER PRIMARY KEY, list BLOB);
CREATE TABLE categories (app_id INTEGER PRIMARY KEY, categories BLOB);
"""


def payload(title="Quarterly report", sender="Alex Smith",
            body="Please review the numbers before Friday.", category=MAIL,
            record_uuid=b"\0" * 16, date=0.0):
    """A stored Outlook notification, with the keys Outlook really sends."""
    request = {"iden": "AAMkAGI2THVSAAA=", "dest": 15, "titl": title,
               "subt": sender, "body": body,
               "filcr": "00000000-0000-0000-0000-000000000000",
               "usda": b"bplist00", "soun": {"nam": "newmail"},
               "cate": category, "thre": "AAQkAGI2TH"}
    request = {key: value for key, value in request.items()
               if value is not None}
    return plistlib.dumps(
        {"styl": 1, "app": "com.microsoft.Outlook", "uuid": record_uuid,
         "date": date, "srce": b"\1" * 16, "req": request, "orig": 2},
        fmt=plistlib.FMT_BINARY)


def mac_now():
    return time.time() - otb.MAC_EPOCH


class NotificationCenter:
    """The database, written the way macOS writes it: in WAL mode, by a
    connection of its own that stays open while the bridge reads."""
    OUTLOOK = 76
    OTHER = 12

    def __init__(self, path, outlook=True):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(SCHEMA)
        self.con.executemany("INSERT INTO dbinfo VALUES (?, ?)",
                             [("compatibleVersion", "17"), ("version", "19")])
        self.con.execute("INSERT INTO app (app_id, identifier) VALUES (?, ?)",
                         (self.OTHER, "com.apple.mail"))
        if outlook:
            self.register_outlook()
        self.con.commit()

    def register_outlook(self):
        self.con.execute("INSERT INTO app (app_id, identifier) VALUES (?, ?)",
                         (self.OUTLOOK, "com.microsoft.outlook"))
        self.con.commit()

    def post(self, title="Quarterly report", sender="Alex Smith",
             body="Please review the numbers before Friday.", category=MAIL,
             app=None, at=None, data=None):
        """Store a notification; returns its key as the bridge sees it."""
        at = mac_now() if at is None else at
        record_uuid = uuid.uuid4().bytes
        if data is None:
            data = payload(title, sender, body, category, record_uuid, at)
        self.con.execute(
            "INSERT INTO record (app_id, uuid, data, request_date, "
            "request_last_date, delivered_date, presented, style) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 1)",
            (app or self.OUTLOOK, record_uuid, data, at, at, at))
        self.con.commit()
        return record_uuid.hex()

    def close(self):
        self.con.close()


RESULTS = {
    "getMe": {"id": 4242, "is_bot": True, "first_name": "Mail",
              "username": "mail_test_bot"},
    "getChat": {"id": 42, "type": "private", "first_name": "Alex",
                "username": "alex"},
    "getChatMember": {"status": "member"},
    "getUpdates": [],
    "sendMessage": {"message_id": 1},
}


class FakeTelegram:
    """The Bot API methods the bridge calls. Records every call; answers
    from a queue per method, or with success when the queue is empty.

    It also works as an HTTP proxy for http:// API addresses, which is how
    the tests see that telegram.proxy is used."""

    def __init__(self, token=TOKEN):
        self.token = token
        self.calls = []
        self.queued = {}
        self.lock = threading.Lock()
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.server.serve_forever, args=(0.05,),
                         daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def reply(self, method, status, body):
        """Answer the next call of method with this status and body."""
        with self.lock:
            self.queued.setdefault(method, []).append((status, body))

    def calls_of(self, method):
        with self.lock:
            return [call for call in self.calls if call["method"] == method]

    def sent(self):
        return [call["fields"] for call in self.calls_of("sendMessage")]

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def answer(self, call):
        with self.lock:
            self.calls.append(call)
            queue = self.queued.get(call["method"])
            if queue:
                return queue.pop(0)
        if call["token"] != self.token:
            return 401, {"ok": False, "error_code": 401,
                         "description": "Unauthorized"}
        return 200, {"ok": True, "result": RESULTS.get(call["method"], True)}

    def handler(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                fields = dict(urllib.parse.parse_qsl(
                    self.rfile.read(size).decode(), keep_blank_values=True))
                path = urllib.parse.urlsplit(self.path).path
                match = re.fullmatch(r"/bot([^/]*)/(\w+)", path)
                if match:
                    status, body = fake.answer({
                        "token": match.group(1), "method": match.group(2),
                        "fields": fields,
                        "proxied": self.path.startswith("http://")})
                else:
                    status, body = 404, {"ok": False,
                                         "description": "Not Found"}
                data = body if isinstance(body, bytes) else \
                    json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        return Handler


def error(status, description, **extra):
    return {"ok": False, "error_code": status, "description": description,
            **extra}


def write_config(path, token=TOKEN, **settings):
    """A config file with working Telegram settings and no focus changes,
    updated by settings; dictionaries in settings merge. The token goes to
    its own file next to the config; None removes that file."""
    cfg = {"telegram": {"chat_id": "42"}, "defocus_outlook": {"enabled": False}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(otb.merge(cfg, settings), f, ensure_ascii=False)
    token_file = otb.token_path(path)
    if token is None:
        if os.path.exists(token_file):
            os.remove(token_file)
    else:
        otb.write_secret(token_file, token)
    return path


class TempDirTest(unittest.TestCase):
    def setUp(self):
        # A space in the path, like "Group Containers" in the real one.
        self.tmp = tempfile.mkdtemp(prefix="otb test ")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def config(self, **settings):
        return otb.load_config(write_config(self.path("config.json"),
                                            **settings))
