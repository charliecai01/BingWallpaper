#!/bin/bash
# Run `bing_wallpaper.py --rotate` automatically at midnight and noon via launchd.
#
#   ./auto_rotate.sh install    # register the LaunchAgent (rotates once now)
#   ./auto_rotate.sh uninstall  # remove it
#   ./auto_rotate.sh status     # show whether it's loaded and the last run
#   ./auto_rotate.sh run        # what launchd calls; rotates once per half-day
#
# launchd fires this at 00:00, 12:00 and at login. If the Mac was asleep at a
# trigger, launchd runs the missed job on wake; if it was off, the login run
# covers it. The `run` step allows one rotation per half-day (00:00-11:59,
# 12:00-23:59), so later logins in the same half don't change it again.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.charliecai.bingwallpaper.rotate"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/bing-wallpaper-rotate.log"
STAMP="$REPO_DIR/.last_rotate"

# Half-day slot ("YYYY-MM-DD am|pm") of epoch $1, or of now if omitted.
slot() {
    local day hour
    read -r day hour < <(date ${1:+-r "$1"} '+%F %H')
    if (( 10#$hour < 12 )); then echo "$day am"; else echo "$day pm"; fi
}

run() {
    cd "$REPO_DIR"
    if [[ -f "$STAMP" && "$(slot "$(stat -f %m "$STAMP")")" == "$(slot)" ]]; then
        echo "$(date '+%F %T') already rotated this half-day, skipping."
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
    <array>
        <dict>
            <key>Hour</key>
            <integer>0</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
        <dict>
            <key>Hour</key>
            <integer>12</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
    </array>
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
