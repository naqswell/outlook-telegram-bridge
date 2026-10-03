#!/usr/bin/python3
"""Forward Microsoft Outlook notifications from a Mac to Telegram.

Outlook posts a macOS notification for each new mail and calendar reminder,
and macOS stores it in the Notification Center database. This daemon reads
that database and sends every new Outlook notification to a Telegram chat
through your bot. It never connects to the mail server and needs no mail
password.

Without options it runs as the daemon. README.md explains the setup.
"""
import argparse
import collections
import copy
import datetime
import fcntl
import getpass
import json
import os
import platform
import plistlib
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

__version__ = "1.0.0"

APP_NAME = "OutlookTelegramBridge"
LABEL = "io.github.naqswell.outlook-telegram-bridge"
CONFIG_PATH = "~/.config/outlook-telegram-bridge/config.json"
# The token sits next to the config, so that the config holds no secret.
TOKEN_FILE = "bot_token"
STATE_DIR = "~/.local/state/outlook-telegram-bridge"
PLIST_PATH = f"~/Library/LaunchAgents/{LABEL}.plist"
MAC_EPOCH = 978307200  # Notification Center dates count from 2001-01-01 UTC

CONTENT_LEVELS = ("full", "sender_only", "minimal")
DEFAULTS = {
    "telegram": {
        "chat_id": "",
        "thread_id": None,
        "proxy": "",
        "api_url": "https://api.telegram.org",
    },
    "notify_content": "full",
    "message_prefix": "\U0001F4E7 ",
    "calendar_prefix": "\U0001F4C5 ",
    "empty_message": "New mail in Outlook",
    "empty_reminder": "Reminder in Outlook",
    "poll_seconds": 5,
    "defocus_outlook": {"enabled": True, "idle_seconds": 180,
                        "activate": "Finder"},
    "jira": {"base_url": "", "subject_prefix": "[JIRA]"},
    "log_content": False,
    "outlook_bundle_id": "com.microsoft.outlook",
    "db_path": "",
}

TEXT_LIMIT = 4096
UNREADABLE_GRACE = 30  # seconds an unreadable record gets to be complete
UNREADABLE_TEXT = "Outlook notification the bridge could not read"
FORGET_AFTER = 86400  # seconds a handled notification must be gone from the
# database before it is forgotten
STATUS_EVERY = 60  # seconds between status.json writes when nothing changed
STALE_AFTER = 900  # seconds after which --check calls a silent agent stuck
TELEGRAM_RETRIES = 3
TELEGRAM_WAIT_MAX = 60
DEFOCUS_EVERY = 15
DESKTOP_CHECK_EVERY = 300
BACKOFF_MAX = 300
CONFIG_RETRY = 30

FDA_HINT = (
    f"Give {APP_NAME}.app Full Disk Access: System Settings > Privacy & "
    "Security > Full Disk Access, add the app from /Applications (or "
    "~/Applications) and switch it on. Then restart the bridge: "
    f"launchctl kickstart -k gui/$(id -u)/{LABEL}")
TERMINAL_DB_HINT = (
    "This terminal may not read the database, and the bridge does not need "
    f"it to: the bridge reads it as {APP_NAME}.app. Only --preview and --once "
    "need a terminal with Full Disk Access; without one, skip them")
NETWORK_HINT = (
    "Check the network. Where Telegram is blocked, set telegram.proxy to an "
    "HTTP proxy, see README.md, section Telegram bot")
DESKTOP_WARNING = (
    "Telegram is running on this Mac. While it shows the chat the bridge "
    "writes to, it marks new messages as read and your phone stays silent. "
    "Quit it, or keep that chat closed.")


def log(message):
    try:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)
    except OSError:
        # A full disk or a closed stdout must not stop the forwarding: a
        # failure between a send and its bookkeeping would repeat the mail.
        pass


def mac_time(seconds):
    """A Notification Center date as local time."""
    if seconds is None:
        return "unknown time"
    return datetime.datetime.fromtimestamp(
        seconds + MAC_EPOCH).strftime("%Y-%m-%d %H:%M")


# Config ---------------------------------------------------------------------

class ConfigError(Exception):
    pass


TOKEN_RE = re.compile(r"[0-9]{3,}:[A-Za-z0-9_-]{20,}")
TOKEN_IN_TEXT_RE = re.compile(
    r"(?<![\w-])[0-9]{3,}:[A-Za-z0-9_-]{20,}(?![\w-])")
CHAT_RE = re.compile(r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{3,}")
URL_RE = re.compile(r"https?://[^\s/]+(/\S*)?")
PROXY_RE = re.compile(r"https?://[^\s/]+/?")


def merge(base, override):
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def unknown_keys(given, known, prefix=""):
    found = []
    for key, value in given.items():
        if key.startswith("_"):
            continue
        if key not in known:
            found.append(prefix + key)
        elif isinstance(known[key], dict) and isinstance(value, dict):
            found.extend(unknown_keys(value, known[key], f"{prefix}{key}."))
    return found


def token_path(config_path):
    return os.path.join(os.path.dirname(config_path), TOKEN_FILE)


def read_token(path):
    """The bot token saved by --set-token, or "" when there is none yet."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from None


def write_secret(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = path + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(temporary, path)


def read_config(path):
    """The config merged over DEFAULTS, with the bot token from its own
    file, the values not checked yet."""
    try:
        with open(path, encoding="utf-8") as f:
            given = json.load(f)
    except FileNotFoundError:
        raise ConfigError(
            f"no config at {path}; ./install.sh creates one") from None
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON: {e}") from None
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    if not isinstance(given, dict):
        raise ConfigError(f"{path} must hold a JSON object")
    if isinstance(given.get("telegram"), dict) \
            and "bot_token" in given["telegram"]:
        raise ConfigError(
            f"telegram.bot_token does not belong in {path}: the token lives "
            f"in {token_path(path)}. Remove it from the config and save it "
            "with --set-token")
    typos = unknown_keys(given, DEFAULTS)
    if typos:
        raise ConfigError(
            f"unknown setting {', '.join(typos)} in {path}; "
            "config.example.json and README.md list the valid ones")
    cfg = merge(DEFAULTS, given)
    if isinstance(cfg["telegram"], dict):
        for key in ("chat_id", "proxy", "api_url"):
            if isinstance(cfg["telegram"][key], str):
                cfg["telegram"][key] = cfg["telegram"][key].strip()
        cfg["telegram"]["bot_token"] = read_token(token_path(path))
    return cfg


def encodable(text):
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def topic_number(value):
    """A topic number written as 5 or "5", else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if (isinstance(value, str) and value.isascii() and value.isdigit()
            and int(value) > 0):
        return int(value)
    return None


def telegram_problems(tg, need_chat=True):
    problems = []
    token = tg["bot_token"]
    if not token:
        problems.append(
            "no bot token yet: create a bot with @BotFather and save its "
            "token with --set-token, see README.md, section Telegram bot")
    elif not TOKEN_RE.fullmatch(token):
        problems.append(
            "the saved bot token does not look like one, which is digits, a "
            "colon and about 35 more characters; save it again with "
            "--set-token")
    chat = tg["chat_id"]
    if chat in ("", None):
        if need_chat:
            problems.append(
                "telegram.chat_id is empty: write to your bot in Telegram, "
                "then run --find-chat")
    elif isinstance(chat, bool) or not (
            isinstance(chat, int)
            or (isinstance(chat, str) and CHAT_RE.fullmatch(chat))):
        problems.append(
            "telegram.chat_id must be a chat number such as 123456789 or "
            "-1001234567890, or a @channelname")
    thread = tg["thread_id"]
    if thread not in (None, "") and topic_number(thread) is None:
        problems.append(
            "telegram.thread_id must be null or a topic number, the last "
            "part of a topic link https://t.me/c/<group>/<topic>")
    proxy = tg["proxy"]
    if proxy and not (isinstance(proxy, str) and PROXY_RE.fullmatch(proxy)):
        problems.append(
            "telegram.proxy must be empty or an address such as "
            "http://127.0.0.1:8080")
    api = tg["api_url"]
    if not (isinstance(api, str) and URL_RE.fullmatch(api)):
        problems.append(
            "telegram.api_url must be an address such as "
            "https://api.telegram.org")
    return problems


def config_problems(cfg, need_chat=True):
    """Every problem of a merged config, each saying how to fix it."""
    problems = []
    if isinstance(cfg["telegram"], dict):
        problems += telegram_problems(cfg["telegram"], need_chat)
    else:
        problems.append('telegram must be an object: {"chat_id": "..."}')
    if cfg["notify_content"] not in CONTENT_LEVELS:
        problems.append("notify_content must be full, sender_only or minimal")
    for key in ("message_prefix", "calendar_prefix", "empty_message",
                "empty_reminder", "db_path"):
        if not isinstance(cfg[key], str):
            problems.append(f"{key} must be text")
        elif not encodable(cfg[key]):
            problems.append(f"{key} holds a broken character, such as half "
                            "of an emoji written as \\ud83d")
    if not (isinstance(cfg["outlook_bundle_id"], str)
            and cfg["outlook_bundle_id"]):
        problems.append("outlook_bundle_id must be a bundle id such as "
                        "com.microsoft.outlook")
    if not is_number(cfg["poll_seconds"]) or cfg["poll_seconds"] <= 0:
        problems.append("poll_seconds must be a number above 0")
    if not isinstance(cfg["log_content"], bool):
        problems.append("log_content must be true or false")
    defocus = cfg["defocus_outlook"]
    if not isinstance(defocus, dict):
        problems.append('defocus_outlook must be an object: {"enabled": true, '
                        '"idle_seconds": 180, "activate": "Finder"}')
    else:
        if not isinstance(defocus["enabled"], bool):
            problems.append("defocus_outlook.enabled must be true or false")
        if not is_number(defocus["idle_seconds"]) \
                or defocus["idle_seconds"] < 10:
            problems.append("defocus_outlook.idle_seconds must be a number "
                            "of seconds, 10 or more")
        if not (isinstance(defocus["activate"], str)
                and defocus["activate"].strip()):
            problems.append("defocus_outlook.activate must name an app, "
                            "such as Finder")
    jira = cfg["jira"]
    if not (isinstance(jira, dict) and all(
            isinstance(jira[key], str)
            for key in ("base_url", "subject_prefix"))):
        problems.append('jira must be {"base_url": "https://...", '
                        '"subject_prefix": "..."} with text values')
    elif jira["base_url"] and not URL_RE.fullmatch(jira["base_url"]):
        problems.append("jira.base_url must be empty or a full address such "
                        "as https://jira.example.com")
    return problems


def load_config(path, need_chat=True):
    cfg = read_config(path)
    problems = config_problems(cfg, need_chat)
    if problems:
        raise ConfigError("\n".join(problems))
    cfg["telegram"]["thread_id"] = topic_number(cfg["telegram"]["thread_id"])
    cfg["db_path"] = (os.path.expanduser(cfg["db_path"]) if cfg["db_path"]
                      else default_db_path())
    return cfg


# Notification Center database -----------------------------------------------

def darwin_user_dir():
    try:
        result = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_DIR"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    folder = result.stdout.strip()
    return folder if result.returncode == 0 and folder else None


def default_db_path():
    """Where this macOS keeps the Notification Center database.

    Current macOS keeps it in a group container, older releases under
    DARWIN_USER_DIR.
    """
    current = os.path.expanduser(
        "~/Library/Group Containers/group.com.apple.usernoted/db2/db")
    if os.path.exists(current):
        return current
    older = darwin_user_dir()
    if older:
        path = os.path.join(older, "com.apple.notificationcenter", "db2", "db")
        if os.path.exists(path):
            return path
    return current


def connect(db_path):
    # Opened afresh for every poll: a connection kept open can hold on to an
    # old snapshot of the write-ahead log and miss new notifications.
    return sqlite3.connect(f"file:{urllib.parse.quote(db_path)}?mode=ro",
                           uri=True, timeout=5)


def outlook_app_id(con, bundle_id):
    row = con.execute(
        "SELECT app_id FROM app WHERE lower(identifier) = lower(?)",
        (bundle_id,)).fetchone()
    return row[0] if row else None


def stored_records(con, app_id):
    """{key: delivered_date} of every delivered Outlook notification that
    the database holds now."""
    return {record_key(uuid): delivered for uuid, delivered in con.execute(
        "SELECT uuid, delivered_date FROM record "
        "WHERE app_id = ? AND delivered_date IS NOT NULL", (app_id,))}


def new_records(con, app_id, keys):
    """(uuid, delivered_date, data) of the notifications among keys, oldest
    first."""
    rows = con.execute(
        "SELECT uuid, delivered_date, data FROM record "
        "WHERE app_id = ? AND delivered_date IS NOT NULL "
        "ORDER BY delivered_date, rec_id", (app_id,)).fetchall()
    return [row for row in rows if record_key(row[0]) in keys]


def record_key(uuid):
    return uuid.hex() if isinstance(uuid, (bytes, bytearray)) else str(uuid)


Notification = collections.namedtuple(
    "Notification", "title subtitle body category")


def repair_utf16(blob):
    """blob with each unpaired UTF-16 surrogate in its strings replaced by
    U+FFFD, or blob unchanged when it is not a binary plist this can walk.

    A preview cut after a fixed number of UTF-16 units can split an emoji in
    two, and plistlib then refuses the whole record. The layout walked here is
    Apple's binary plist: a trailer with the offset table's place, offsets to
    every object, and strings marked 0x6N holding N UTF-16 units.
    """
    try:
        if not blob.startswith(b"bplist00") or len(blob) < 40:
            return blob
        offset_size = blob[-26]
        count = int.from_bytes(blob[-24:-16], "big")
        table = int.from_bytes(blob[-8:], "big")
        # A record cut short or padded has a trailer that points anywhere,
        # and walking it could run for ever: the offset table has to end
        # where the trailer starts.
        if not (1 <= offset_size <= 8 and 8 <= table
                and table + count * offset_size == len(blob) - 32):
            return blob
        fixed = bytearray(blob)
        for i in range(count):
            at = table + i * offset_size
            offset = int.from_bytes(blob[at:at + offset_size], "big")
            if not 8 <= offset < table or blob[offset] >> 4 != 0x6:
                continue
            length, start = blob[offset] & 0xF, offset + 1
            if length == 0xF:  # the length follows as an integer object
                width = 1 << (blob[start] & 0xF)
                length = int.from_bytes(blob[start + 1:start + 1 + width],
                                        "big")
                start += 1 + width
            end = start + 2 * length
            if end > table:
                continue
            text = blob[start:end].decode("utf-16-be", "surrogatepass")
            fixed[start:end] = "".join(
                "\ufffd" if "\ud800" <= char <= "\udfff" else char
                for char in text).encode("utf-16-be")
        return bytes(fixed)
    except Exception:
        return blob


def parse_record(blob):
    """The text of a stored notification; ValueError when unreadable."""
    try:
        root = plistlib.loads(blob)
    except Exception:
        try:
            root = plistlib.loads(repair_utf16(blob))
        except Exception as e:
            raise ValueError(
                f"not a property list ({type(e).__name__})") from None
    request = root.get("req") if isinstance(root, dict) else None
    if not isinstance(request, dict):
        raise ValueError("no notification request inside")

    def text(key):
        value = request.get(key)
        return value if isinstance(value, str) else None

    return Notification(text("titl"), text("subt"), text("body"), text("cate"))


# Message text ---------------------------------------------------------------

def utf16_len(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def clip(text, limit=TEXT_LIMIT):
    """text cut to Telegram's 4096-character limit. Counting UTF-16 units
    keeps it under the limit whichever way Telegram counts."""
    if utf16_len(text) <= limit:
        return text
    used = 0
    for i, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit - 1:
            return text[:i] + "…"
    return text


def is_calendar(category):
    return isinstance(category, str) and "reminder" in category.lower()


def format_message(cfg, note):
    """The text to send for a notification, or None when it has no text."""
    # Outlook puts the subject in titl and the sender in subt: the sender is
    # note.subtitle.
    fields = [note.subtitle, note.title, note.body]
    if not any(field and field.strip() for field in fields):
        return None
    calendar = is_calendar(note.category)
    prefix = cfg["calendar_prefix"] if calendar else cfg["message_prefix"]
    placeholder = cfg["empty_reminder"] if calendar else cfg["empty_message"]
    level = cfg["notify_content"]
    if level == "minimal":
        fields = []
    elif level == "sender_only":
        fields = [note.subtitle]
    parts = [field.strip() for field in fields if field and field.strip()]
    return clip(prefix + (" · ".join(parts) if parts else placeholder))


ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9_]+-[0-9]+\b")
PATH_WORD_RE = re.compile(r"\S*/\S*")


def issue_links(cfg, title, text):
    """(start, end, url) for each mention in text of the Jira issue that the
    mail subject names.

    Only mail whose subject starts with jira.subject_prefix qualifies, since
    another Jira can have a project with the same key. Only the subject's own
    key is linked: previews are cut mid-word (PAY-10 out of PAY-1027), mail
    quotes other trackers, and ISO-8583 looks like a key too. A key inside a
    link or a path belongs to that link.
    """
    jira = cfg["jira"]
    base = jira["base_url"].rstrip("/")
    title = title or ""
    if not base or not title.startswith(jira["subject_prefix"]):
        return []
    named = ISSUE_KEY_RE.search(title)
    if not named:
        return []
    paths = [m.span() for m in PATH_WORD_RE.finditer(text)]
    return [(m.start(), m.end(), f"{base}/browse/{m.group()}")
            for m in ISSUE_KEY_RE.finditer(text)
            if m.group() == named.group()
            and not any(start <= m.start() < end for start, end in paths)]


# Telegram -------------------------------------------------------------------

class Reply(collections.namedtuple("Reply", "status payload error")):
    """One Bot API answer; status is None when no HTTP answer came."""
    __slots__ = ()

    @property
    def ok(self):
        return (self.status == 200 and isinstance(self.payload, dict)
                and self.payload.get("ok") is True)

    @property
    def result(self):
        return self.payload.get("result") if self.ok else None


def failure_hint(status, description):
    description = description.lower()
    if status in (401, 404):
        return ("The bot token is wrong or was revoked; copy it again from "
                "@BotFather")
    if status == 403:
        return ("The bot may not write to this chat: unblock the bot, or add "
                "it back to the group")
    if status == 409:
        return ("Another program reads this bot's updates, or the bot has a "
                "webhook; give the bridge a bot of its own")
    if status == 400 and "chat not found" in description:
        return ("The bot has not seen this chat: write to the bot, or add it "
                "to the group, and check telegram.chat_id")
    if status == 400 and "thread" in description:
        return "There is no such topic: check telegram.thread_id"
    return None


def failure_text(status, payload, body):
    description = (payload.get("description")
                   if isinstance(payload, dict) else None)
    if not isinstance(payload, dict):
        # A proxy or a captive portal answered, not Telegram. Its page may
        # quote the request URL, token included, so it is not shown.
        return (f"HTTP {status} with something other than a Bot API answer "
                f"({len(body)} bytes): a proxy or a network login page may be "
                f"in the way. {NETWORK_HINT}")
    if status == 200:
        return f"Telegram did not accept the request: {description}"
    text = f"HTTP {status}" + (f": {description}" if description else "")
    hint = failure_hint(status, description or "")
    return f"{text}. {hint}" if hint else text


def read_quietly(response):
    try:
        return response.read()
    except Exception:
        return b""


def retry_after(payload):
    """Seconds a 429 asks to wait, kept within 1..TELEGRAM_WAIT_MAX."""
    try:
        seconds = int(payload["parameters"]["retry_after"])
    except (TypeError, KeyError, ValueError):
        seconds = 5
    return min(max(seconds, 1), TELEGRAM_WAIT_MAX)


def telegram_entities(text, links):
    # Bot API offsets count UTF-16 code units: the prefix 📧 alone is two.
    def at(index):
        return utf16_len(text[:index])
    return [{"type": "text_link", "offset": at(start),
             "length": at(end) - at(start), "url": url}
            for start, end, url in links]


PROXY_VARIABLES = ("https_proxy", "http_proxy", "all_proxy")


def system_proxies():
    """The proxies set in System Settings. The background agent sees only
    these, so a command in a terminal ignores the terminal's HTTPS_PROXY too
    and fails the same way the agent would."""
    read = getattr(urllib.request, "getproxies_macosx_sysconf", None)
    try:
        return read() if read else {}
    except Exception:
        return {}


class Telegram:
    def __init__(self, tg, sleep=time.sleep):
        self.token = tg["bot_token"]
        self.chat_id = tg["chat_id"]
        self.thread_id = tg["thread_id"]
        self.api = tg["api_url"].rstrip("/")
        proxy = tg["proxy"]
        proxies = ({"http": proxy, "https": proxy} if proxy
                   else system_proxies())
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies))
        self.sleep = sleep

    def redact(self, text):
        if not self.token:
            return text
        for form in {self.token, urllib.parse.quote(self.token, safe=""),
                     urllib.parse.quote_plus(self.token)}:
            text = text.replace(form, "<bot token>")
        return text

    def call(self, method, **fields):
        url = f"{self.api}/bot{self.token}/{method}"
        data = urllib.parse.urlencode(fields).encode()
        try:
            with self.opener.open(url, data=data, timeout=30) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, read_quietly(e)
        except Exception as e:
            return Reply(None, None, self.redact(
                f"no answer from Telegram: {type(e).__name__}: {e}. "
                f"{NETWORK_HINT}"))
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        reply = Reply(status, payload, "")
        if reply.ok:
            return reply
        return reply._replace(
            error=self.redact(failure_text(status, payload, body)))

    def send(self, text, links=(), topic=True, attempt=0, fallback=True):
        """Send one message to the configured chat. Returns (ok, error).

        With fallback, a message Telegram refuses for the topic goes to the
        group's main chat rather than nowhere."""
        fields = {"chat_id": self.chat_id, "text": text,
                  "link_preview_options": json.dumps({"is_disabled": True})}
        thread = self.thread_id if topic else None
        if thread:
            fields["message_thread_id"] = thread
        if links:
            fields["entities"] = json.dumps(telegram_entities(text, links))
        reply = self.call("sendMessage", **fields)
        if reply.ok:
            return True, ""
        if reply.status == 429 and attempt < TELEGRAM_RETRIES:
            # A group takes about 20 bot messages a minute, and a burst of
            # mail runs into that.
            wait = retry_after(reply.payload)
            log(f"Telegram asked to slow down; sending again in {wait}s")
            self.sleep(wait)
            return self.send(text, links, topic, attempt + 1, fallback)
        # A 400 means nothing was sent, so sending again cannot duplicate.
        if reply.status == 400 and links:
            log(f"Telegram refused the message with links ({reply.error}); "
                "sending it without them")
            return self.send(text, (), topic, attempt, fallback)
        if reply.status == 400 and thread and fallback:
            log(f"Telegram refused the message for topic {thread} "
                f"({reply.error}); sending it to the group's main chat")
            return self.send(text, (), False, attempt)
        return False, reply.error


# Forwarding -----------------------------------------------------------------

def read_state(path):
    """(seen, last), or (None, None) when there is no usable state.

    seen holds the keys of the notifications already handled, last the date
    of the latest forwarded one.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data["seen"], list):
            raise TypeError("seen is not a list")
        last = data.get("last")
        return ({str(key) for key in data["seen"]},
                None if last is None else float(last))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(temporary, path)


def write_state(path, seen, last):
    write_json(path, {"seen": sorted(seen), "last": last})


def read_status(path):
    """What the running daemon last reported about itself, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            status = json.load(f)
    except (OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


class Bridge:
    """Hands new Outlook notifications to send(), oldest first.

    A notification is new while its key is not in seen. That leaves the clock
    out: a notification dated in the future, or written after a newer one,
    still goes out once. seen drops a key a day after the database did, so it
    stays about as small as the database.
    """

    def __init__(self, cfg, state_path, send, now=None):
        self.cfg = cfg
        self.state_path = state_path
        self.send = send
        self.started = time.time() - MAC_EPOCH if now is None else now
        self.seen, self.last = read_state(state_path)
        if self.seen is None and os.path.exists(state_path):
            log(f"cannot use {state_path}, it is damaged or unreadable; "
                "starting afresh, as on a first run")
        self.waiting_for_outlook = False
        self.delivery = None  # "ok" or the error of the latest attempt
        self.unreadable = {}  # key -> when it was first found unreadable
        self.missing = {}  # key -> when it was first found gone

    def poll(self):
        """One pass. False when a delivery failed and must be retried."""
        with closing(connect(self.cfg["db_path"])) as con:
            app_id = outlook_app_id(con, self.cfg["outlook_bundle_id"])
            stored = {} if app_id is None else stored_records(con, app_id)
            if self.seen is None:
                # The first run skips what came before the bridge started,
                # and forwards what came after even if Full Disk Access
                # arrived later than the bridge.
                self.seen = {key for key, delivered in stored.items()
                             if delivered < self.started}
                self.save()
                log("first run: forwarding Outlook notifications from now on")
            if app_id is None:
                if not self.waiting_for_outlook:
                    log("Outlook has not posted a notification on this Mac "
                        "yet; waiting for the first one")
                    self.waiting_for_outlook = True
                return True
            self.waiting_for_outlook = False
            present = set(stored)
            self.forget_gone(present)
            self.unreadable = {key: since for key, since
                               in self.unreadable.items() if key in present}
            new = present - self.seen
            rows = new_records(con, app_id, new) if new else []
        if not rows and self.delivery not in (None, "ok"):
            self.delivery = None  # the mail that failed is gone
        return self.forward(rows)

    def forget_gone(self, present):
        """Drop from seen what the database has not held for a day. Right
        away would send a notification again that vanishes for a moment."""
        now = time.monotonic()
        for key in self.seen - present:
            self.missing.setdefault(key, now)
        for key in list(self.missing):
            if key in present or key not in self.seen:
                del self.missing[key]
        forgotten = {key for key, since in self.missing.items()
                     if now - since >= FORGET_AFTER}
        if forgotten:
            self.seen -= forgotten
            for key in forgotten:
                del self.missing[key]
            self.save()

    def forward(self, rows):
        for index, (uuid, delivered, blob) in enumerate(rows):
            key = record_key(uuid)
            if key in self.seen:
                continue  # the database held this uuid twice
            try:
                note = parse_record(blob)
            except ValueError as e:
                since = self.unreadable.setdefault(key, time.monotonic())
                if time.monotonic() - since < UNREADABLE_GRACE:
                    # It may still be being written. Start the clock of the
                    # unreadable ones behind it too, so that they wait
                    # together, not one after another.
                    for other in rows[index + 1:]:
                        try:
                            parse_record(other[2])
                        except ValueError:
                            self.unreadable.setdefault(
                                record_key(other[0]), time.monotonic())
                    return True
                log(f"an Outlook notification cannot be read ({e}); "
                    "forwarding a placeholder")
                note = None
            if note is None:
                text, links = self.cfg["message_prefix"] + UNREADABLE_TEXT, []
            else:
                text = format_message(self.cfg, note)
                if text is None:
                    log("skipped an Outlook notification without text")
                    self.done(key, None)
                    continue
                links = self.links(note, text)
            ok, error = self.send(text, links)
            self.delivery = "ok" if ok else error
            if not ok:
                log(f"Telegram delivery failed, will retry: {error}")
                return False
            self.done(key, delivered)
            log(self.summary(note))
        return True

    def links(self, note, text):
        try:
            return issue_links(self.cfg, note.title, text)
        except Exception as e:
            # The links are an extra; a bug in them must not hold the mail
            # back.
            log(f"Jira links skipped: {type(e).__name__}: {e}")
            return []

    def summary(self, note):
        if note is None:
            return "forwarded a placeholder for an unreadable notification"
        kind = "calendar reminder" if is_calendar(note.category) else "mail"
        if not self.cfg["log_content"]:
            return f"forwarded {kind}"
        return (f"forwarded {kind}: title={note.title!r} "
                f"subtitle={note.subtitle!r} body={note.body!r} "
                f"category={note.category!r}")

    def done(self, key, delivered):
        self.seen.add(key)
        self.unreadable.pop(key, None)
        if delivered is not None:
            self.last = delivered
        self.save()

    def save(self):
        write_state(self.state_path, self.seen, self.last)


# The Mac around the bridge --------------------------------------------------

def frontmost_bundle_id(run=subprocess.run):
    """Bundle id of the frontmost app. Launch Services answers without any
    permission, unlike System Events."""
    try:
        asn = run(["/usr/bin/lsappinfo", "front"], capture_output=True,
                  text=True, timeout=10).stdout.strip()
        if not asn:
            return None
        info = run(["/usr/bin/lsappinfo", "info", "-only", "bundleid", asn],
                   capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'"CFBundleIdentifier"="([^"]+)"', info)
    return match.group(1) if match else None


def input_idle_seconds(run=subprocess.run):
    """Seconds since the last keyboard or mouse input. Input over Screen
    Sharing counts."""
    try:
        out = run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
                  capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(match.group(1)) / 1e9 if match else None


def nudge_focus_off_outlook(cfg, frontmost=frontmost_bundle_id,
                            idle=input_idle_seconds, run=subprocess.run):
    """Bring another app forward when Outlook is in front and nobody has
    touched the Mac for a while.

    Mail that arrives while Outlook is the active app produces no
    notification, so there would be nothing to forward. Nothing is hidden or
    closed; Outlook only stops being the active app.
    """
    conf = cfg["defocus_outlook"]
    if not conf["enabled"]:
        return False
    front = frontmost()
    if not front or front.lower() != cfg["outlook_bundle_id"].lower():
        return False
    seconds = idle()
    if seconds is None or seconds < conf["idle_seconds"]:
        return False
    target = conf["activate"]
    try:
        result = run(["/usr/bin/open", "-a", target], capture_output=True,
                     text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"could not bring {target} forward: {e}")
        return False
    if result.returncode != 0:
        log(f"could not bring {target} forward: {result.stderr.strip()}")
        return False
    log(f"Outlook was in front with no input for {int(seconds)}s; brought "
        f"{target} forward so new mail shows notifications again")
    return True


TELEGRAM_DESKTOP = r"/Telegram( Desktop)?\.app/Contents/MacOS/"


def telegram_desktop_running(run=subprocess.run):
    try:
        result = run(["/usr/bin/pgrep", "-f", TELEGRAM_DESKTOP],
                     capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def launchd_state(run=subprocess.run):
    """"running, pid N", "not loaded", another launchd state, or None when
    launchctl is not available."""
    try:
        result = run(["/bin/launchctl", "print",
                      f"gui/{os.getuid()}/{LABEL}"],
                     capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return "not loaded"
    state = re.search(r"^\s*state = (.+?)\s*$", result.stdout, re.M)
    pid = re.search(r"^\s*pid = (\d+)", result.stdout, re.M)
    return ((state.group(1) if state else "loaded")
            + (f", pid {pid.group(1)}" if pid else ""))


def find_outlook():
    for folder in ("/Applications", os.path.expanduser("~/Applications")):
        path = os.path.join(folder, "Microsoft Outlook.app")
        if os.path.isdir(path):
            return path
    return None


def chat_name(chat):
    if chat.get("title"):
        name = f'"{chat["title"]}"'
    else:
        name = " ".join(part for part in (chat.get("first_name"),
                                          chat.get("last_name")) if part)
    if chat.get("username"):
        name = (f"{name} (@{chat['username']})" if name
                else f"@{chat['username']}")
    return f"{chat.get('type', 'chat')} {name}".strip()


# Commands -------------------------------------------------------------------

class Checklist:
    def __init__(self):
        self.failed = False

    def add(self, level, name, detail):
        self.failed = self.failed or level == "FAIL"
        print(f"{level:<4}  {name}: {detail}", flush=True)


def check_config(out, path):
    """Report on the config; return it merged, or None when unreadable."""
    try:
        cfg = read_config(path)
    except ConfigError as e:
        out.add("FAIL", "config", str(e))
        return None
    problems = config_problems(cfg)
    for problem in problems:
        out.add("FAIL", "config", problem)
    if not problems:
        out.add("PASS", "config", path)
    secret = token_path(path)
    if os.path.exists(secret) and os.stat(secret).st_mode & 0o077:
        out.add("WARN", "config", f"other users can read the bot token in "
                f"{secret}: chmod 600 {shlex.quote(secret)}")
    return cfg


def check_database(out, cfg):
    """The database as this terminal sees it. The bridge itself reads it with
    the permission of the app, which the background agent line reports."""
    db_path, bundle = "", DEFAULTS["outlook_bundle_id"]
    if cfg is not None:
        if isinstance(cfg["db_path"], str):
            db_path = os.path.expanduser(cfg["db_path"])
        if isinstance(cfg["outlook_bundle_id"], str) \
                and cfg["outlook_bundle_id"]:
            bundle = cfg["outlook_bundle_id"]
    db_path = db_path or default_db_path()
    try:
        with closing(connect(db_path)) as con:
            app_id = outlook_app_id(con, bundle)
            count, latest = con.execute(
                "SELECT count(*), max(delivered_date) FROM record "
                "WHERE app_id = ?", (app_id,)).fetchone()
    except sqlite3.Error as e:
        out.add("INFO", "notification database",
                f"this terminal cannot read {db_path} ({e}). The bridge reads "
                f"it as {APP_NAME}.app, and the background agent line shows "
                "whether that works")
        return
    out.add("PASS", "notification database",
            f"this terminal can read {db_path}")
    if app_id is None:
        out.add("WARN", "Outlook notifications",
                "Outlook has not posted a notification on this Mac yet. "
                "Allow notifications for Microsoft Outlook in System "
                "Settings > Notifications, then send yourself a mail")
    elif count:
        out.add("PASS", "Outlook notifications",
                f"{count} stored, the latest from {mac_time(latest)}")
    else:
        out.add("PASS", "Outlook notifications",
                "Outlook is registered, no notification is stored right now")


def check_telegram(out, tg):
    if not tg["proxy"]:
        names = sorted({name.upper() for name, value in os.environ.items()
                        if name.lower() in PROXY_VARIABLES and value})
        if names:
            out.add("INFO", "Telegram proxy", f"this terminal sets "
                    f"{', '.join(names)}, which the bridge ignores: the "
                    "background agent would not get it. If Telegram needs a "
                    "proxy here, put it into telegram.proxy")
    bot = Telegram(tg)
    me = bot.call("getMe")
    if not me.ok:
        out.add("FAIL", "Telegram bot", me.error)
        return
    out.add("PASS", "Telegram bot", f"@{me.result.get('username')}")
    if tg["chat_id"] in ("", None):
        return
    chat = bot.call("getChat", chat_id=tg["chat_id"])
    if not chat.ok:
        out.add("FAIL", "Telegram chat", chat.error)
        return
    info = chat.result
    out.add("PASS", "Telegram chat", chat_name(info))
    if info.get("type") != "private":
        member = bot.call("getChatMember", chat_id=tg["chat_id"],
                          user_id=me.result.get("id"))
        status = member.result.get("status") if member.ok else None
        if not member.ok:
            out.add("WARN", "Telegram chat",
                    f"cannot see the bot's membership: {member.error}")
        elif status in ("left", "kicked"):
            out.add("FAIL", "Telegram chat",
                    "the bot is not a member of this chat; add it back")
        elif info.get("type") == "channel" and status != "administrator":
            out.add("FAIL", "Telegram chat",
                    "a bot posts to a channel only as an administrator")
        elif status == "restricted" \
                and not member.result.get("can_send_messages", True):
            out.add("FAIL", "Telegram chat",
                    "the bot may not send messages in this group")
    thread = topic_number(tg["thread_id"])
    if thread and info.get("is_forum"):
        out.add("INFO", "Telegram topic",
                f"thread_id {thread}; Telegram offers no way to check a topic "
                "without posting, --selftest shows where messages land")
    elif thread:
        out.add("WARN", "Telegram topic",
                f"thread_id {thread} is set, but this chat has no topics; "
                "messages go to the chat itself")


def check_agent(out, state_dir):
    """The background agent, and what it last reported about itself. Only
    the agent runs with the app's Full Disk Access: a command typed in a
    terminal runs with the terminal's."""
    state = launchd_state()
    plist = os.path.expanduser(PLIST_PATH)
    if state is None:
        out.add("INFO", "background agent", "launchctl is not available")
        return
    if state == "not loaded" and os.path.exists(plist):
        out.add("INFO", "background agent", "not started; start it with: "
                f"launchctl bootstrap gui/$(id -u) {plist}")
        return
    if state == "not loaded":
        out.add("INFO", "background agent", "not installed; run ./install.sh")
        return
    running = re.fullmatch(r"running, pid (\d+)", state)
    if not running:
        out.add("WARN", "background agent", f"loaded but {state}; its log is "
                f"{os.path.join(state_dir, 'agent.log')}")
        return
    status = read_status(os.path.join(state_dir, "status.json"))
    log_path = os.path.join(state_dir, "agent.log")
    if not status or int(running.group(1)) not in (status.get("pid"),
                                                   status.get("parent_pid")):
        out.add("INFO", "background agent", f"{state}; it has not reported "
                "yet, run --check again in a few seconds")
        return
    silent = time.time() - float(status.get("updated") or 0)
    if silent >= STALE_AFTER:
        out.add("WARN", "background agent", f"{state}, but it has not "
                f"reported for {int(silent // 60)} minutes and may be stuck. "
                f"Read its log, {log_path}, then restart it: "
                f"launchctl kickstart -k gui/$(id -u)/{LABEL}")
    elif status.get("error"):
        out.add("FAIL", "background agent", f"{state}, but its last pass "
                f"failed: {status['error']}. The log has the details: "
                f"{log_path}")
    elif status.get("database") is None:
        out.add("INFO", "background agent", f"{state}; it has not reported "
                "yet, run --check again in a few seconds")
    elif status["database"] != "ok":
        out.add("FAIL", "background agent", f"{state}, but it cannot read the "
                f"notification database: {status['database']}. {FDA_HINT}")
    elif status.get("outlook") is False:
        out.add("WARN", "background agent", f"{state}; it reads the "
                "notification database, but Outlook has not posted a "
                "notification on this Mac yet. Allow notifications for "
                "Microsoft Outlook in System Settings > Notifications")
    elif status.get("delivery") not in (None, "ok"):
        out.add("FAIL", "background agent", f"{state}; its latest delivery "
                f"to Telegram failed: {status['delivery']}")
    else:
        out.add("PASS", "background agent", f"{state}; it reads the "
                "notification database"
                + (" and forwards to Telegram"
                   if status.get("delivery") == "ok" else ""))


def run_check(config_path, state_dir):
    """Read-only diagnostics. Exit status 1 when any line is a FAIL."""
    out = Checklist()
    if sys.platform == "darwin":
        out.add("PASS", "macOS", platform.mac_ver()[0] or "version unknown")
    else:
        out.add("FAIL", "macOS",
                f"the bridge runs only on macOS, this is {sys.platform}")
    outlook = find_outlook()
    out.add("PASS" if outlook else "WARN", "Outlook",
            outlook or "Microsoft Outlook.app is not in /Applications")
    cfg = check_config(out, config_path)
    check_database(out, cfg)
    if cfg is not None and isinstance(cfg["telegram"], dict) \
            and not telegram_problems(cfg["telegram"], need_chat=False):
        check_telegram(out, cfg["telegram"])
    if telegram_desktop_running():
        out.add("WARN", "Telegram on this Mac", DESKTOP_WARNING)
    check_agent(out, state_dir)
    seen, last = read_state(os.path.join(state_dir, "state.json"))
    if seen is None:
        progress = ("no state yet; the first run forwards notifications from "
                    "then on")
    elif last is None:
        progress = "nothing forwarded yet"
    else:
        progress = ("the latest forwarded notification is from "
                    f"{mac_time(last)}")
    out.add("INFO", "progress", progress)
    return 1 if out.failed else 0


def run_set_token(config_path, source=None):
    """Save the bot token next to the config. It is read from stdin, typed
    hidden on a terminal, and never printed, so that it stays out of
    terminal scrollback and out of an AI agent's transcript."""
    source = source or sys.stdin
    if source.isatty():
        given = getpass.getpass("Bot token from @BotFather (stays hidden): ")
    else:
        given = source.read()
    if not given.strip():
        print("Nothing came in. Copy the token first, or run --set-token in "
              "Terminal without a pipe and paste it at the prompt. Nothing "
              "was saved.")
        return 1
    # The whole BotFather message may come in: take the token out of it.
    found = TOKEN_IN_TEXT_RE.search(given)
    if not found:
        print("There is no bot token in that, which is digits, a colon and "
              "about 35 more characters. Nothing was saved.")
        return 1
    token = found.group()
    tg = dict(DEFAULTS["telegram"], bot_token=token)
    try:
        configured = read_config(config_path)["telegram"]
    except ConfigError:
        configured = None
    if isinstance(configured, dict):
        proxy, api = configured["proxy"], configured["api_url"]
        if isinstance(proxy, str) and PROXY_RE.fullmatch(proxy):
            tg["proxy"] = proxy
        if isinstance(api, str) and URL_RE.fullmatch(api):
            tg["api_url"] = api
    me = Telegram(tg).call("getMe")
    if me.status in (401, 404):
        print(f"Telegram does not know this token: {me.error}. Nothing was "
              "saved.")
        return 1
    path = token_path(config_path)
    write_secret(path, token + "\n")
    if me.ok:
        print(f"Saved the token of @{me.result.get('username')} to {path}.")
    else:
        print(f"Saved the token to {path}, but Telegram could not confirm "
              f"it: {me.error}")
    if launchd_state() not in (None, "not loaded"):
        print("The background agent still uses the old token; restart it: "
              f"launchctl kickstart -k gui/$(id -u)/{LABEL}")
    return 0


def run_find_chat(cfg):
    bot = Telegram(cfg["telegram"])
    me = bot.call("getMe")
    if not me.ok:
        print(f"Telegram: {me.error}")
        return 1
    name = f"@{me.result.get('username')}"
    updates = bot.call("getUpdates")
    if not updates.ok:
        print(f"Telegram: {updates.error}")
        return 1
    chats = collections.OrderedDict()
    for update in updates.result or []:
        for key in ("message", "edited_message", "channel_post",
                    "my_chat_member"):
            item = update.get(key)
            if not isinstance(item, dict) \
                    or not isinstance(item.get("chat"), dict):
                continue
            topics = chats.setdefault(item["chat"].get("id"),
                                      (item["chat"], set()))[1]
            if item.get("is_topic_message") and item.get("message_thread_id"):
                topics.add(item["message_thread_id"])
    # In groups Telegram passes a bot only commands and replies to it, so a
    # plain post in a topic never shows up here.
    topic_tip = (f"For a topic of a group, add {name} to the group and send "
                 f"/start@{name[1:]} inside the topic, then run --find-chat "
                 "again.")
    if not chats:
        print(f"{name} has no messages yet. In Telegram, write anything to "
              f"{name}. {topic_tip}")
        return 1
    print(f"Chats that wrote to {name} in the last 24 hours:")
    for chat_id, (chat, topics) in chats.items():
        line = f"  chat_id {chat_id}  {chat_name(chat)}"
        if topics:
            line += "  topics: " + ", ".join(
                f"thread_id {topic}" for topic in sorted(topics))
        print(line)
    print("Use a chat of your own: everyone in it will read your mail.")
    if any(chat.get("type") != "private" and not topics
           for chat, topics in chats.values()):
        print(topic_tip)
    return 0


def run_preview(cfg, count):
    try:
        with closing(connect(cfg["db_path"])) as con:
            app_id = outlook_app_id(con, cfg["outlook_bundle_id"])
            rows = con.execute(
                "SELECT delivered_date, data FROM record WHERE app_id = ? "
                "AND delivered_date IS NOT NULL "
                "ORDER BY delivered_date DESC LIMIT ?",
                (app_id, count)).fetchall()
    except sqlite3.Error as e:
        print(f"cannot read {cfg['db_path']}: {e}. {TERMINAL_DB_HINT}.")
        return 1
    if not rows:
        print("No Outlook notification is stored right now. Send yourself a "
              "mail and run --preview again.")
        return 0
    for delivered, blob in reversed(rows):
        stamp = mac_time(delivered)
        try:
            note = parse_record(blob)
        except ValueError as e:
            print(f"{stamp}  unreadable, would be skipped: {e}")
            continue
        text = format_message(cfg, note)
        if text is None:
            print(f"{stamp}  no text, would be skipped")
            continue
        print(f"{stamp}  {text}")
        for start, end, url in issue_links(cfg, note.title, text):
            print(f"{'':16}  link {text[start:end]} -> {url}")
    print("Nothing was sent.")
    return 0


def run_selftest(cfg):
    """Send a test message. Only Telegram is tested: reading notifications
    is the background agent's part, and --check reports on it."""
    if telegram_desktop_running():
        print(DESKTOP_WARNING)
    # A new text each run: iOS stops showing banners for a repeated message.
    text = (f"{cfg['message_prefix']}Outlook to Telegram bridge test, "
            f"{time.strftime('%H:%M:%S')}")
    tg = cfg["telegram"]
    where = f"chat {tg['chat_id']}" + (
        f", topic {tg['thread_id']}" if tg["thread_id"] else "")
    # No fallback to the main chat here: a wrong topic must show up now,
    # not as mail landing in the group's main chat later.
    ok, error = Telegram(tg).send(text, fallback=False)
    if ok:
        print(f"Telegram: test message sent to {where}. Check that it is "
              "there and that your phone showed a notification for it.")
        return 0
    print(f"Telegram: the test message to {where} failed: {error}")
    return 1


def run_once(cfg, state_path):
    bridge = Bridge(cfg, state_path, Telegram(cfg["telegram"]).send)
    try:
        return 0 if bridge.poll() else 1
    except sqlite3.Error as e:
        log(f"cannot read the notification database: {e}. "
            f"{TERMINAL_DB_HINT}.")
        return 1


class Stop(BaseException):
    """Raised on SIGTERM. A BaseException, so that no except Exception on
    the way swallows it."""


def raise_stop(signum, frame):
    raise Stop()


def orphaned():
    """True when the app that started this daemon is gone."""
    return os.environ.get("OTB_VIA_APP") == "1" and os.getppid() == 1


def wait_for_config(path, sleep=time.sleep):
    """The config, read again every CONFIG_RETRY seconds while it has
    problems; None once the app that started the daemon is gone. Exiting
    instead would make launchd restart the daemon every 10 seconds, and a
    fixed config is picked up without a restart."""
    reported = None
    while not orphaned():
        try:
            return load_config(path)
        except ConfigError as e:
            if str(e) != reported:
                log(f"config problem, waiting for it to be fixed:\n{e}")
                reported = str(e)
        for _ in range(CONFIG_RETRY):  # by the second, to notice an orphan
            if orphaned():
                break
            sleep(1)
    log(f"{APP_NAME}.app is gone; stopping")
    return None


def run_loop(cfg, state_path):
    log(f"outlook-telegram-bridge {__version__} started; reading "
        f"{cfg['db_path']} every {cfg['poll_seconds']}s")
    bridge = Bridge(cfg, state_path, Telegram(cfg["telegram"]).send)
    desktop = telegram_desktop_running()
    if desktop:
        log(DESKTOP_WARNING)
    next_desktop_check = time.monotonic() + DESKTOP_CHECK_EVERY
    next_defocus = time.monotonic()
    status_path = os.path.join(os.path.dirname(state_path), "status.json")
    failures = 0
    reported = written = database = None
    written_at = 0.0
    while True:
        error = unexpected = None
        try:
            delivered = bridge.poll()
            database = "ok"
        except sqlite3.Error as e:
            delivered, database = True, str(e)
            error = f"cannot read the notification database: {e}. {FDA_HINT}"
        except Exception as e:
            delivered = True
            unexpected = f"{type(e).__name__}: {e}"
            error = "unexpected error:\n" + traceback.format_exc()
        if error != reported:
            if error:
                log(error)
            reported = error
        failures = 0 if delivered else min(failures + 1, 10)
        # --check reads this: a terminal cannot see what the app may read.
        status = {"pid": os.getpid(), "parent_pid": os.getppid(),
                  "version": __version__, "database": database,
                  "outlook": (not bridge.waiting_for_outlook
                              if database == "ok" else None),
                  "delivery": bridge.delivery, "error": unexpected}
        if status != written or time.time() - written_at >= STATUS_EVERY:
            try:
                write_json(status_path, dict(status, updated=time.time()))
                written, written_at = status, time.time()
            except OSError as e:
                log(f"cannot write {status_path}: {e}")
        now = time.monotonic()
        if cfg["defocus_outlook"]["enabled"] and now >= next_defocus:
            next_defocus = now + DEFOCUS_EVERY
            try:
                nudge_focus_off_outlook(cfg)
            except Exception as e:
                log(f"focus check failed: {type(e).__name__}: {e}")
        if now >= next_desktop_check:
            next_desktop_check = now + DESKTOP_CHECK_EVERY
            running = telegram_desktop_running()
            if running and not desktop:
                log(DESKTOP_WARNING)
            desktop = running
        if orphaned():
            log(f"{APP_NAME}.app is gone; stopping")
            return
        poll = cfg["poll_seconds"]
        time.sleep(max(poll, min(poll * 2 ** failures, BACKOFF_MAX)))


def run_daemon(config_path, state_path):
    signal.signal(signal.SIGTERM, raise_stop)
    try:
        cfg = wait_for_config(config_path)
        if cfg is not None:
            run_loop(cfg, state_path)
    except (Stop, KeyboardInterrupt):
        log("stopped")
    return 0


def take_lock(state_dir):
    """An open lock file descriptor, or None when another copy holds it."""
    os.makedirs(state_dir, exist_ok=True)
    fd = os.open(os.path.join(state_dir, "bridge.lock"),
                 os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="outlook-telegram-bridge", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    command = parser.add_mutually_exclusive_group()
    command.add_argument(
        "--check", action="store_true",
        help="read-only diagnostics, one PASS/WARN/FAIL/INFO line each; "
             "exit status 1 on any FAIL")
    command.add_argument(
        "--set-token", action="store_true",
        help="save the bot token, read from stdin, next to the config; it "
             "is never printed")
    command.add_argument(
        "--find-chat", action="store_true",
        help="list the chats that wrote to the bot, with the chat_id for "
             "the config")
    command.add_argument(
        "--preview", type=int, nargs="?", const=3, metavar="N",
        help="print the latest N Outlook notifications (3 by default) as "
             "they would be sent; sends nothing")
    command.add_argument(
        "--selftest", action="store_true",
        help="send one test message to the configured chat")
    command.add_argument(
        "--once", action="store_true",
        help="forward what is new, then exit")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    config_path = os.path.expanduser(os.environ.get("OTB_CONFIG")
                                     or CONFIG_PATH)
    state_dir = os.path.expanduser(os.environ.get("OTB_STATE_DIR")
                                   or STATE_DIR)
    state_path = os.path.join(state_dir, "state.json")

    if args.check:
        return run_check(config_path, state_dir)
    if args.set_token:
        return run_set_token(config_path)
    daemon = not (args.find_chat or args.selftest or args.once
                  or args.preview is not None)
    if daemon or args.once:
        lock = take_lock(state_dir)  # held until the process exits
        if lock is None:
            log("another copy of the bridge is running already; this one "
                "exits")
            return 3
        if daemon:
            return run_daemon(config_path, state_path)
    try:
        cfg = load_config(config_path, need_chat=not args.find_chat)
    except ConfigError as e:
        print(f"config problem in {config_path}:\n{e}", file=sys.stderr)
        return 2
    if args.find_chat:
        return run_find_chat(cfg)
    if args.preview is not None:
        return run_preview(cfg, max(1, args.preview))
    if args.selftest:
        return run_selftest(cfg)
    return run_once(cfg, state_path)


if __name__ == "__main__":
    sys.exit(main())
