#!/bin/bash
# Keep a menu bar button running that rotates the wallpaper on click.
#
#   ./auto_menubar.sh install    # compile menubar.swift, register the LaunchAgent
#   ./auto_menubar.sh uninstall  # remove the button
#   ./auto_menubar.sh status     # show whether it's loaded
#
# launchd starts the button at login and restarts it if it dies. Clicks run
# `bing_wallpaper.py --rotate` directly, so they aren't limited to one per
# half-day like the scheduled job in auto_rotate.sh.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.charliecai.bingwallpaper.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/bing-wallpaper-menubar.log"
BINARY="$REPO_DIR/.menubar"

install() {
    local python
    python="$(command -v python3)"
    swiftc -O "$REPO_DIR/menubar.swift" -o "$BINARY"
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BINARY</string>
        <string>$REPO_DIR</string>
        <string>$python</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Installed $PLIST (python: $python)"
    echo "Log: $LOG"
}

uninstall() {
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$BINARY"
    echo "Uninstalled $LABEL"
}

status() {
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        echo "Loaded: $LABEL"
    else
        echo "Not loaded"
    fi
    [[ -f "$LOG" ]] && { echo "--- tail $LOG"; tail -n 10 "$LOG"; }
    return 0
}

case "${1:-}" in
    install|uninstall|status) "$1" ;;
    *) echo "usage: $0 {install|uninstall|status}" >&2; exit 1 ;;
esac
