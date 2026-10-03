#!/bin/bash
# Removes outlook-telegram-bridge. Keeps the config and the state unless
# --purge is given. OTB_APP_DIR and OTB_BUNDLE_ID mean the same as in
# install.sh; with OTB_APP_DIR set, only that folder is searched for the app.
set -euo pipefail

BUNDLE_ID="${OTB_BUNDLE_ID:-io.github.naqswell.outlook-telegram-bridge}"
APP_NAME="OutlookTelegramBridge"

say() { printf '%s\n' "$*"; }
die() { printf 'uninstall.sh: %s\n' "$*" >&2; exit 1; }

[ -n "${HOME:-}" ] || die "HOME is not set"
[ "$(id -u)" -ne 0 ] || die "run it as yourself, without sudo"
case "${1:-}" in
  ""|--purge) ;;
  *) die "unknown option $1; the only option is --purge" ;;
esac

if [ -n "${OTB_APP_DIR:-}" ]; then
  APPS=("$OTB_APP_DIR/$APP_NAME.app")
else
  APPS=("/Applications/$APP_NAME.app" "$HOME/Applications/$APP_NAME.app")
fi
LIBEXEC="$HOME/.local/libexec/outlook-telegram-bridge"
CONFIG_DIR="$HOME/.config/outlook-telegram-bridge"
STATE_DIR="$HOME/.local/state/outlook-telegram-bridge"
PLIST="$HOME/Library/LaunchAgents/$BUNDLE_ID.plist"

say "==> Stopping the background agent"
launchctl bootout "gui/$(id -u)/$BUNDLE_ID" 2>/dev/null || true

say "==> Removing the app, the daemon and the agent"
rm -f "$PLIST"
rm -rf "${APPS[@]}" "$LIBEXEC"

if [ "${1:-}" = --purge ]; then
  rm -rf "$CONFIG_DIR" "$STATE_DIR"
  say "    removed the config and the state too"
else
  say "    kept $CONFIG_DIR, with the bot token, and $STATE_DIR;"
  say "    --purge removes them"
fi

say ""
say "The Full Disk Access entry stays in System Settings > Privacy & Security >"
say "Full Disk Access. Select $APP_NAME there and press - to remove it."
