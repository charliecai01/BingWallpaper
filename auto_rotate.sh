#!/bin/bash
# Run `bing_wallpaper.py --rotate` automatically at midnight via launchd.
#
#   ./auto_rotate.sh install    # register the LaunchAgent (rotates once now)
#   ./auto_rotate.sh uninstall  # remove it
#   ./auto_rotate.sh status     # show whether it's loaded and the last run
#   ./auto_rotate.sh run        # what launchd calls; syncs Photos, rotates once a day
#
# launchd fires this at 00:00 and at login. If the Mac was asleep at midnight,
# launchd runs the missed job on wake; if it was off, the login run covers it.
# The `run` step allows one rotation per calendar day, so logins later that
# day don't change the wallpaper again.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.charliecai.bingwallpaper.rotate"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/bing-wallpaper-rotate.log"
STAMP="$REPO_DIR/.last_rotate"

run() {
    cd "$REPO_DIR"
    # Idempotent and cheap when nothing is new, so it runs on every trigger.
    python3 bing_wallpaper.py --sync-photos || echo "$(date '+%F %T') photos sync failed"
    if [[ -f "$STAMP" && "$(date -r "$(stat -f %m "$STAMP")" +%F)" == "$(date +%F)" ]]; then
        echo "$(date '+%F %T') already rotated today, skipping."
        return 0
    fi
    echo "$(date '+%F %T') rotating wallpaper"
    python3 bing_wallpaper.py --rotate
    touch "$STAMP"
}

install() {
    local python
    python="$(command -v python3)"
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
        <string>/bin/bash</string>
        <string>$REPO_DIR/auto_rotate.sh</string>
        <string>run</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "$python"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>0</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
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
    rm -f "$PLIST"
    echo "Uninstalled $LABEL"
}

status() {
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        echo "Loaded: $LABEL"
    else
        echo "Not loaded"
    fi
    if [[ -f "$STAMP" ]]; then
        echo "Last rotation: $(date -r "$(stat -f %m "$STAMP")" '+%F %T')"
    else
        echo "Last rotation: never"
    fi
    [[ -f "$LOG" ]] && { echo "--- tail $LOG"; tail -n 10 "$LOG"; }
    return 0
}

case "${1:-}" in
    run|install|uninstall|status) "$1" ;;
    *) echo "usage: $0 {install|uninstall|status|run}" >&2; exit 1 ;;
esac
