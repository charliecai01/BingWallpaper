# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single Python script (stdlib only, no dependencies) that fetches Bing's daily "picture of the day" and maintains a local archive: a full-history markdown log (`bing-wallpaper.md`), a rolling 30-day gallery in `README.md`, a rolling 90-day cache of the actual full-resolution (3840x2160) JPEGs in `wallpapers/`, and a rolling 90-day list of 1080x1920 portrait URLs in `phone-wallpapers.txt` for the iPhone.

Fetching runs in GitHub Actions: `.github/workflows/update-archive.yml` runs `--no-download` every 3 days (`0 0 */3 * *`, plus `workflow_dispatch`) and commits `bing-wallpaper.md`, `README.md` and `phone-wallpapers.txt` as `github-actions[bot]`. Every 3 days is well inside Bing's ~8-day window. Running the script manually and pushing also works for a fresher pull. Locally, two launchd jobs run, and neither fetches from Bing or commits: `auto_backfill.sh` runs `--backfill` every 3 days (pulls and caches already-recorded images), and `auto_rotate.sh` runs `--rotate` at midnight and noon (sets the desktop picture).

The iPhone is handled by an iOS Shortcuts personal automation ("Bing Wallpaper", 12AM and 12PM), not by anything in this repo: it downloads `phone-wallpapers.txt` from `raw.githubusercontent.com/charliecai01/BingWallpaper/main/`, picks the first URL not in its own `bing_shown.txt` (iCloud Drive/Shortcuts), sets it with "Set Wallpaper Photo", and clears that file once everything has been shown. iOS has no API for an app to set the wallpaper, which is why it's a Shortcut. It sees new days whenever the GitHub Action (or a manual push) updates the file, and the repo must stay public.

## Commands

```bash
python3 bing_wallpaper.py
python3 bing_wallpaper.py --backfill   # git pull, then cache the last 90 days of recorded images
python3 bing_wallpaper.py --rotate     # set a random never-shown cached image as the desktop picture
./auto_backfill.sh install|uninstall|status|run
./auto_rotate.sh install|uninstall|status|run
```

`auto_rotate.sh` follows the same pattern: a LaunchAgent (`com.charliecai.bingwallpaper.rotate`) firing at 00:00, 12:00 and at login, with `run` allowing one rotation per half-day (am/pm) via `.last_rotate` (gitignored). Log: `~/Library/Logs/bing-wallpaper-rotate.log`.

`auto_backfill.sh install` writes a LaunchAgent (`~/Library/LaunchAgents/com.charliecai.bingwallpaper.backfill.plist`) that fires daily at 10:00 and at login; the `run` step skips unless `.last_backfill` (gitignored timestamp file) is ≥3 days old. A plain 3-day `StartInterval` is avoided on purpose — reboots reset it. The plist embeds the repo's absolute path and the resolved `python3`, so re-run `install` after moving the repo. The repo must live outside TCC-protected folders (`~/Documents`, `~/Desktop`, `~/Downloads`) — launchd jobs there fail with "Operation not permitted". Log: `~/Library/Logs/bing-wallpaper-backfill.log`.

Requires Python 3.10+ (uses `list[Image]` type hints and `dataclass`). No install step, no tests or linters configured.

## Architecture

Everything lives in `bing_wallpaper.py`, run top-to-bottom from `main()`:

1. `read_wallpaper_list()` parses the existing `bing-wallpaper.md` back into `Image` objects via a regex matching its own output format — the file is the source of truth for history, not a database.
2. `fetch_recent()` requests Bing's `HPImageArchive.aspx` API with `n=8` (`uhd=1&uhdwidth=3840&uhdheight=2160`) and parses **all** returned entries, not just today's. **This matters**: Bing's live API is not a real archive — it only ever exposes a short rolling window (~8 days), so pulling all of it every run means a gap of up to ~8 days between runs still self-heals. Skip the tool for longer than that and those days are gone for good; there's no way to recover them from Bing afterward. Do not revert this to fetching only `idx=0` ("today").
3. The new entries are prepended to the list and `write_wallpaper_list()` rewrites `bing-wallpaper.md` in full every run, deduping — **this file keeps unbounded full history**, never trimmed. `dedupe()` keeps one record per date, preferring EN-US: before `mkt=en-US` was pinned in `BING_API`, Bing sometimes served a day under ROW/ZH-CN too, occasionally with a different picture.
4. Separately, `filter_recent()` trims the list to the last `README_DAYS` (30) and `write_readme()` rewrites `README.md` from that filtered list — the README is a rolling window, not history.
5. `download_wallpaper()` saves each newly-fetched image into `wallpapers/<date>.jpg` (skipping ones already on disk), then `prune_wallpaper_dir()` deletes any file in `wallpapers/` older than 90 days, keyed off the filename's date.
6. `--backfill` first runs `git pull` (a failure only warns and continues with local records), then skips the API fetch entirely and instead downloads every image already recorded in `bing-wallpaper.md` from the last `RETENTION_DAYS` days into `wallpapers/` — useful for populating the cache from history (e.g. first-time setup) since the live API can't reach back that far itself.
7. `--rotate` touches neither the archive nor the network: it picks a random `wallpapers/*.jpg` whose date isn't in `.shown_wallpapers` (gitignored, cleared each cycle), sets it on every screen, and appends the date. When all cached images have been shown it deletes `.shown_wallpapers` and starts a new cycle (skipping the image currently on screen), so nothing repeats until everything has been seen. It sets the picture via `NSWorkspace` through `osascript -l JavaScript`'s ObjC bridge rather than `tell application "System Events"`, because the bridge sends no Apple Events and so needs no Automation (TCC) permission under launchd.
8. On every fetch (Action or manual), `write_phone_list()` rewrites `phone-wallpapers.txt` with the `portrait_url()` (Bing's native 1080x1920 shot, `_UHD.jpg` → `_1080x1920.jpg`) of each of the last `RETENTION_DAYS` records, newest first, one per line — the iPhone Shortcut's only input. It replaced an older `--sync-photos` that imported portraits into a Photos album for Photo Shuffle; that cluttered the photo library, since Photos' AppleScript can't delete items.

`Image` (a frozen dataclass: `date`, `desc`, `url`) has four different string renderings, one per consumer above: `markdown()` for `bing-wallpaper.md` rows, `table_cell()` for the README's 3-column table cells (thumbnail + a "download 4k" link), `large_url()` for the README's hero image at the top, and `portrait_url()` for `phone-wallpapers.txt`.

### The URL trick that matters

Bing's API returns an image URL with query params attached; `fetch_today()` truncates it at the first `&` (`url.split("&", 1)[0]`). That bare URL — `.../th?id=OHR.xxxxx_UHD.jpg` with **no** `w`/`h` params — is what gets stored as `Image.url` and is what `download_wallpaper()` fetches. This has been verified to return genuine 3840x2160 at higher quality (less compression) than explicitly appending `&w=3840&h=2160`. Do not "fix" this by adding explicit width/height params — that would make downloads worse, not better. Thumbnail params (`&pid=hp&w=384&h=216&rs=1&c=4`) are added separately in `Image.table_cell()` only for the small README table previews, and `&w=960` only for the README hero image via `large_url()`.

### Two different retention rules, on purpose

`bing-wallpaper.md` (full history, text-only, cheap to keep), `wallpapers/` + `phone-wallpapers.txt` (`RETENTION_DAYS`, 90 days, because the binary JPEGs are the expensive part) and `README.md` (`README_DAYS`, 30 days, to keep the gallery short) are intentionally inconsistent in scope. When changing `RETENTION_DAYS`, remember it only affects `backfill()`, `write_phone_list()` and `prune_wallpaper_dir()` — `read_wallpaper_list()`/`write_wallpaper_list()` are untouched by it.

`wallpapers/` is gitignored — the JPEGs are a local cache regenerated by running the script, not meant to be committed.

## History

This project was originally a Java/Maven tool (Hutool HTTP client) auto-run daily via GitHub Actions cron with an auto-commit step. It was rewritten as this dependency-free Python script, gained a local 4K image cache (now 90 days), and the Action now runs every 3 days without downloading images.
