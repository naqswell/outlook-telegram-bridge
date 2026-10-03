#!/bin/bash
# Installs outlook-telegram-bridge for the current user. Safe to run again.
# It starts nothing and grants no permission; it ends by printing what is left.
#
# OTB_APP_DIR picks the folder for the app (default /Applications, or
# ~/Applications when /Applications is not writable). OTB_BUNDLE_ID exists
# for the tests, which install under a name of their own.
set -euo pipefail

BUNDLE_ID="${OTB_BUNDLE_ID:-io.github.naqswell.outlook-telegram-bridge}"
APP_NAME="OutlookTelegramBridge"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
# Text for a plist: & and < in a path would break the XML.
xml() { local s=${1//&/&amp;}; s=${s//</&lt;}; printf '%s' "${s//>/&gt;}"; }
lint() { local out; out="$(plutil -lint "$1")" || die "$out"; }

[ -n "${HOME:-}" ] || die "HOME is not set"
[ "$(id -u)" -ne 0 ] || die "run it as yourself, without sudo"
[ "$(uname -s)" = Darwin ] || die "the bridge runs only on macOS"
xcode-select -p >/dev/null 2>&1 \
  || die "the Command Line Tools are missing: run  xcode-select --install  and try again"
for tool in clang codesign plutil; do
  command -v "$tool" >/dev/null || die "$tool is missing: run  xcode-select --install"
done
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || die "/usr/bin/python3 must be 3.9 or newer: update the Command Line Tools"

if [ -n "${OTB_APP_DIR:-}" ]; then
  APP_DIR="$OTB_APP_DIR"
  mkdir -p "$APP_DIR"
elif [ -w /Applications ]; then
  APP_DIR=/Applications
else
  APP_DIR="$HOME/Applications"
fi
APP="$APP_DIR/$APP_NAME.app"
EXE="$APP/Contents/MacOS/$APP_NAME"
LIBEXEC="$HOME/.local/libexec/outlook-telegram-bridge"
CONFIG_DIR="$HOME/.config/outlook-telegram-bridge"
CONFIG="$CONFIG_DIR/config.json"
STATE_DIR="$HOME/.local/state/outlook-telegram-bridge"
PLIST="$HOME/Library/LaunchAgents/$BUNDLE_ID.plist"

mkdir -p "$LIBEXEC" "$CONFIG_DIR" "$STATE_DIR" "$APP_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$CONFIG_DIR" "$STATE_DIR"

say "==> Daemon: $LIBEXEC"
install -m 0755 "$SRC/src/outlook_telegram_bridge.py" "$LIBEXEC/outlook_telegram_bridge.py"

say "==> Config: $CONFIG"
if [ -f "$CONFIG" ]; then
  chmod 600 "$CONFIG"
  say "    kept the existing config"
else
  install -m 0600 "$SRC/config.example.json" "$CONFIG"
  say "    created from config.example.json"
fi

say "==> App: $APP"
# Full Disk Access belongs to this exact signed app. Building it again changes
# its signature and macOS silently drops the grant, so the app is rebuilt only
# when its Info.plist differs, and the Info.plist carries a hash of the
# launcher source. A fresh build cannot be compared with the installed binary:
# signing rewrites the binary.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
BUILD="$STAGE/$APP_NAME.app"
mkdir -p "$BUILD/Contents/MacOS"
LAUNCHER_SHA="$(shasum -a 256 "$SRC/src/launcher.c" | cut -d ' ' -f 1)"
# The versions below stay fixed: changing them rebuilds the app and costs
# its Full Disk Access.
cat > "$BUILD/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleExecutable</key><string>$APP_NAME</string>
	<key>CFBundleIdentifier</key><string>$(xml "$BUNDLE_ID")</string>
	<key>CFBundleName</key><string>$APP_NAME</string>
	<key>CFBundlePackageType</key><string>APPL</string>
	<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
	<key>CFBundleShortVersionString</key><string>1.0</string>
	<key>CFBundleVersion</key><string>1</string>
	<key>LSBackgroundOnly</key><true/>
	<key>LSUIElement</key><true/>
	<key>OTBLauncherSHA256</key><string>$LAUNCHER_SHA</string>
</dict>
</plist>
PLIST
lint "$BUILD/Contents/Info.plist"

if [ -f "$APP/Contents/Info.plist" ] \
   && cmp -s "$BUILD/Contents/Info.plist" "$APP/Contents/Info.plist" \
   && codesign --verify --strict "$APP" >/dev/null 2>&1; then
  say "    unchanged; kept, so its Full Disk Access stays"
else
  clang -O2 -Wall -Wextra -o "$BUILD/Contents/MacOS/$APP_NAME" "$SRC/src/launcher.c"
  if ! signed="$(codesign --force --sign - --identifier "$BUNDLE_ID" "$BUILD" 2>&1)"; then
    die "codesign failed: $signed"
  fi
  if [ -d "$APP" ]; then
    say "    the launcher changed, replacing the app. macOS drops its Full Disk"
    say "    Access with the old app: grant it again."
  fi
  rm -rf "$APP"
  mv "$BUILD" "$APP"
  say "    built and signed"
fi

say "==> Background agent: $PLIST"
cat > "$STAGE/agent.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>$(xml "$BUNDLE_ID")</string>
	<key>ProgramArguments</key>
	<array>
		<string>$(xml "$EXE")</string>
	</array>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><true/>
	<key>ProcessType</key><string>Background</string>
	<key>StandardOutPath</key><string>$(xml "$STATE_DIR/agent.log")</string>
	<key>StandardErrorPath</key><string>$(xml "$STATE_DIR/agent.log")</string>
</dict>
</plist>
PLIST
lint "$STAGE/agent.plist"
mv "$STAGE/agent.plist" "$PLIST"

if launchctl print "gui/$(id -u)/$BUNDLE_ID" >/dev/null 2>&1; then
  AGENT="loaded; restart it so it runs this version:
           launchctl kickstart -k gui/\$(id -u)/$BUNDLE_ID"
else
  AGENT="not started"
fi

cat <<DONE

Installed.
  app:     $APP
  daemon:  $LIBEXEC/outlook_telegram_bridge.py
  config:  $CONFIG
  agent:   $PLIST
           $AGENT
  log:     $STATE_DIR/agent.log

What is left, from the bot token to starting the agent: README.md, section
"Set it up by hand", or AGENTS.md for an AI agent.
DONE
