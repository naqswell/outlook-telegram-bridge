# Setup runbook for AI agents

You are setting up outlook-telegram-bridge on the user's Mac. This file is the
order of work; README.md is the reference for settings, Telegram details and
troubleshooting. Talk to the user in their language.

## Ground rules

- The bridge needs no mail password and no access to the mail server. A step
  that seems to need either is off track: come back to this runbook.
- The bot token never passes through you. The user copies it, and
  `--set-token` saves it from the clipboard and prints only the bot's name
  (step 5). It lives in `~/.config/outlook-telegram-bridge/bot_token`; leave
  that file unread. `config.json` holds no secret, so read and edit it freely.
  `pbpaste` goes only into the pipe of step 5: run alone, it would print the
  token into this conversation.
- A **user step** is one only the person can do: Telegram on their phone,
  System Settings, saying what their phone showed. Prepare everything, tell
  them exactly what to click or type, then wait for their answer.
- Run the commands this runbook names. The bridge without options is the
  daemon and never returns; `--once` forwards real mail; `--preview` prints
  real mail into this conversation, so ask before it.
- Step 8 starts a background agent that runs at every login. Ask before it.
- When a command needs input you cannot type, or your sandbox blocks it, ask
  the user to run it in Terminal and tell you the result.
- `--check` is the source of truth, and each FAIL line says how to fix it.

Run every command from the root folder of this repository. The commands
below run the bridge's own file, which `install.sh` puts at:

```
~/.local/libexec/outlook-telegram-bridge/outlook_telegram_bridge.py
```

Run from your terminal, it has the terminal's permissions, not the app's. Its
`notification database` line in `--check` describes your terminal, and INFO
there is expected. Only the background agent, started in step 8, reads with
the app's Full Disk Access; the `background agent` line reports what it saw.

## Steps

### 1. Preflight

```sh
sw_vers -productVersion
xcode-select -p
ls -d "/Applications/Microsoft Outlook.app" ~/"Applications/Microsoft Outlook.app" 2>/dev/null
```

When `xcode-select -p` fails, user step: run `xcode-select --install` and
finish its dialog. When `ls` prints no path, stop: the user needs Microsoft
Outlook for Mac first. Its exit status is 1 whenever one of the two folders
lacks Outlook; the printed path is what counts.

User step: ask whether Outlook shows a banner on this Mac when new mail
arrives. If not, they allow notifications for Microsoft Outlook in System
Settings > Notifications, as banners or alerts, and turn on new-mail alerts
in Outlook > Settings, section Notifications or Notifications & Sounds,
depending on the Outlook version. The bridge forwards exactly those banners.

Done when `xcode-select -p` succeeds, `ls` prints a path, and the user
confirms the banners.

### 2. Tests

```sh
/usr/bin/python3 -m unittest discover -s tests
```

They show the code works on this Mac, and they leave nothing installed.

Done when the last line starts with `OK`. `OK (skipped=1)` is fine: tests
skip what this Mac lacks, such as shellcheck.

### 3. Install

```sh
./install.sh
```

It prints a summary and starts nothing. What it leaves to do is this runbook.

Done when it prints `Installed.` Note the path on its `app:` line: step 8
needs it.

### 4. Choices

User step: ask these in one message, each with its default, and wait for the
answers. How much mail leaves the Mac is the user's decision, not yours.

1. Where mail goes: a private chat with their own bot, the default, or a topic
   in a Telegram group. Everyone in that group reads the mail. Step 6 uses
   this answer.
2. How much of each mail may leave the Mac, for `notify_content`: sender,
   subject and preview (`full`, the default), the sender only
   (`sender_only`), or just the fact that mail arrived (`minimal`). Telegram
   keeps bot chats on its servers without end-to-end encryption, and company
   rules may limit what goes there.
3. Whether the bridge may bring Finder forward when Outlook stays in front for
   3 minutes with no input, for `defocus_outlook.enabled`. The default is yes:
   without it, mail that arrives while Outlook is the active app is not
   forwarded.
4. Whether they get mail from Jira. The default is no. If yes, ask for the
   Jira address and for the words such a mail's subject starts with, for
   `jira.base_url` and `jira.subject_prefix`.

Done when the user has answered, and
`~/.config/outlook-telegram-bridge/config.json` holds `notify_content`,
`defocus_outlook.enabled` and, for Jira, the two `jira` settings.

### 5. Bot

User step: in Telegram, open @BotFather, send `/newbot`, and answer its two
questions: a display name, then a username ending in `bot`. BotFather replies
with a token. The user copies it so that it lands in this Mac's clipboard: in
Telegram on this Mac, in web.telegram.org, or on an iPhone with Handoff, which
shares the clipboard. Then save it; the clipboard is cleared only when that
worked:

```sh
pbpaste | ~/.local/libexec/outlook-telegram-bridge/outlook_telegram_bridge.py --set-token && printf '' | pbcopy
```

If it answers `Nothing came in` or that there is no token, the user runs the
same command without `pbpaste |` in Terminal and pastes the token at its
hidden prompt. If they use a clipboard manager, ask them to delete the token
from its history. If the token ever went into a chat, suggest `/revoke` in
BotFather and saving the new one. If they copied it in Telegram on this Mac
or in web.telegram.org, ask them to close it afterwards: step 7 says why.

Done when `--check` shows `PASS  Telegram bot`. Until step 6 it also shows
`FAIL  config: telegram.chat_id is empty`; that one is expected here.

A FAIL with `no answer from Telegram` means no network for the bridge. Check
your sandbox first. The bridge takes a proxy only from `telegram.proxy` or
from System Settings, never from the terminal's `HTTPS_PROXY`, because the
background agent would not have it; `--check` names such a variable in an
INFO line. Ask the user whether Telegram needs a proxy on their network, and
put it into `telegram.proxy`.

### 6. Chat

User step, private chat: open the bot in Telegram and press Start.

User step, group topic: create the group, turn on Topics, add the bot as a
member, and send `/start@<bot username>` inside the topic. In groups Telegram
passes a bot only commands, so a plain message never reaches it.

```sh
~/.local/libexec/outlook-telegram-bridge/outlook_telegram_bridge.py --find-chat
```

Write the `chat_id` it lists into the config, plus the `thread_id` for a
topic. When it lists more than one chat, ask the user which one is theirs:
whoever is in that chat reads the mail.

Done when `--check` shows `PASS  Telegram chat` and no FAIL line for
`Telegram chat`.

### 7. Test message

When `--check` warns that Telegram runs on this Mac, ask the user to quit it
first, and leave the quitting to them. web.telegram.org open in a browser
does the same harm, and `--check` cannot see it: ask about that too.

```sh
~/.local/libexec/outlook-telegram-bridge/outlook_telegram_bridge.py --selftest
```

User step: ask whether the message arrived in the right chat, and topic, and
whether the phone showed a notification with a sound or a banner. A silent
arrival usually means Telegram is open on this Mac, as the app or in
web.telegram.org, or the chat is muted on the phone.

Done when the user confirms both.

### 8. Full Disk Access and start

User step: grant Full Disk Access to the app. Tell them what they grant: the
app may then read every file of their account, and the bridge uses that only
for the notification database. Open the pane, and show them the app, with the
`app:` path from step 3 in the second command:

```sh
open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
open -R "<the app: path from step 3>"
```

They drag the app into the list, or press + and pick it, and turn its switch
on. Wait for their confirmation.

Ask whether to start the background agent. On yes:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.naqswell.outlook-telegram-bridge.plist
```

macOS may announce a new background item: that is this agent. If the command
answers `Bootstrap failed: 5`, run
`launchctl print gui/$(id -u)/io.github.naqswell.outlook-telegram-bridge`.
When that finds the agent, it was loaded already: restart it with
`launchctl kickstart -k gui/$(id -u)/io.github.naqswell.outlook-telegram-bridge`.
When it does not, the user may have switched the item off in System Settings >
General > Login Items: ask them to allow OutlookTelegramBridge there, then run
the bootstrap again.

Done when the `background agent` line of `--check` is PASS, or is the WARN
that Outlook has not posted a notification yet: step 9 brings one. While it
says `has not reported yet`, run `--check` again a few seconds later; after
30 seconds, read `tail -n 30 ~/.local/state/outlook-telegram-bridge/agent.log`,
where a config problem is the usual cause. A FAIL with `authorization denied`
means the access is missing or went to another copy of the app: have the user
fix it, then run the kickstart above.

### 9. Real mail

User step: get a new mail into the inbox. Someone else can send it, or the
user sends one to themselves from Outlook and switches to another app right
after pressing Send: Outlook shows no notification for mail that arrives
while it is the active app.

Done when the mail shows up in Telegram and the `background agent` line of
`--check` ends with `and forwards to Telegram`. When nothing arrives, read
`tail -n 30 ~/.local/state/outlook-telegram-bridge/agent.log` and README.md,
section Troubleshooting.

To show the user how mail would look under another `notify_content`,
`--preview` prints the latest notifications and sends nothing. Ask first: it
puts real mail into this conversation. It needs a terminal that may read the
database; without that, skip it, since the bridge itself does not need it.

### 10. Hand-off

Tell the user:

- the path of this repository folder: keep it for updates and for
  `uninstall.sh`;
- what was installed where: README.md, section What gets installed;
- that the Mac has to stay awake and logged in, with Outlook running and its
  notifications allowed;
- that Telegram on this Mac, as the app or in web.telegram.org, keeps that
  chat closed, or the phone stays silent;
- that `--check` is the first thing to run when mail stops arriving;
- how to remove it: `./uninstall.sh` keeps the config and the bot token,
  `./uninstall.sh --purge` deletes them too; afterwards they remove the app
  from Full Disk Access.

## Later changes

- Settings: edit the config, run `--check` until it shows no FAIL, then
  `launchctl kickstart -k gui/$(id -u)/io.github.naqswell.outlook-telegram-bridge`.
  An agent restarted on a broken config forwards nothing and says so only in
  its log.
- A new version: `git pull`, `./install.sh`, then the same kickstart. When
  `install.sh` says it replaced the app, Full Disk Access has to be granted
  again, as in step 8.
- Changes to the code: run the tests before and after, and keep the README
  tables in step with the code; `tests/test_repo.py` checks that they agree.
