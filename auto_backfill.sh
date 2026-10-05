#!/bin/bash
# Run `bing_wallpaper.py --backfill` automatically every 3 days via launchd.
#
#   ./auto_backfill.sh install    # register the LaunchAgent (runs once now)
#   ./auto_backfill.sh uninstall  # remove it
#   ./auto_backfill.sh status     # show whether it's loaded and the last run
#   ./auto_backfill.sh run        # what launchd calls; skips if run < 3 days ago
#
# launchd fires this daily (and at login); the `run` step itself enforces the
# 3-day spacing using a timestamp file. A plain 3-day StartInterval would be
# reset by every reboot, so on a machine restarted more often than that it
# would never fire.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.charliecai.bingwallpaper.backfill"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/bing-wallpaper-backfill.log"
STAMP="$REPO_DIR/.last_backfill"
INTERVAL_DAYS=3

run() {
    cd "$REPO_DIR"
    if [[ -f "$STAMP" ]]; then
        local age=$(( $(date +%s) - $(stat -f %m "$STAMP") ))
        # One hour of slack so a daily trigger a few seconds early still counts.
        if (( age < INTERVAL_DAYS * 86400 - 3600 )); then
            echo "$(date '+%F %T') last backfill $((age / 3600))h ago, skipping."
            return 0
        fi
    fi
    echo "$(date '+%F %T') starting backfill"
    python3 bing_wallpaper.py --backfill
    touch "$STAMP"
    echo "$(date '+%F %T') backfill done"
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
        <string>$REPO_DIR/auto_backfill.sh</string>
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
        <integer>10</integer>
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
        echo "Last backfill: $(date -r "$(stat -f %m "$STAMP")" '+%F %T')"
    else
        echo "Last backfill: never"
    fi
    [[ -f "$LOG" ]] && { echo "--- tail $LOG"; tail -n 10 "$LOG"; }
    return 0
}

case "${1:-}" in
    run|install|uninstall|status) "$1" ;;
    *) echo "usage: $0 {install|uninstall|status|run}" >&2; exit 1 ;;
esac
