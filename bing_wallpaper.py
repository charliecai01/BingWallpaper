#!/usr/bin/env python3
"""Fetch Bing's picture of the day and update the local archive.

Run manually whenever you want a fresh wallpaper — fetching is never scheduled.
Only --backfill (every 3 days via auto_backfill.sh) and --rotate plus
--sync-photos (nightly at midnight via auto_rotate.sh) run automatically.
"""

import argparse
import json
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

README_PATH = Path("README.md")
WALLPAPER_LIST_PATH = Path("bing-wallpaper.md")
WALLPAPER_DIR = Path("wallpapers")
# Dates (file stems) of every wallpaper --rotate has put on the desktop, so
# none is shown twice. Local to this machine, like wallpapers/ itself.
SHOWN_PATH = Path(".shown_wallpapers")
# Dates already imported into the Photos album by --sync-photos.
PHOTOS_IMPORTED_PATH = Path(".photos_imported")
# Photos album the iPhone's Photo Shuffle wallpaper rotates through, via
# iCloud Photos.
PHOTOS_ALBUM = "Bing Wallpaper"

# Retention window: README.md and wallpapers/ only keep the last 30 days.
# bing-wallpaper.md is untouched by this and keeps the full history.
RETENTION_DAYS = 30

# n=8 pulls Bing's entire available history in one request (idx=0 is today).
# Bing never exposes more than ~8 days back, so fetching all of them every
# run means a gap of up to 8 days between runs still self-heals; skip more
# than that and the missed days are gone for good on Bing's end.
# mkt=en-US pins the market; without it Bing picks one per request and has
# served the same day as ROW, duplicating records (see dedupe()).
BING_API = (
    "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8"
    "&nc=1618537156988&pid=hp&uhd=1&uhdwidth=3840&uhdheight=2160&mkt=en-US"
)
BING_URL = "https://cn.bing.com{}"

# Sets the desktop picture on every screen through NSWorkspace via the JXA
# ObjC bridge. Unlike `tell application "System Events"`, this sends no Apple
# Events, so it needs no Automation permission and works from launchd.
SET_WALLPAPER_JXA = """
ObjC.import('AppKit');
function run(argv) {
    const url = $.NSURL.fileURLWithPath(argv[0]);
    const screens = $.NSScreen.screens;
    for (let i = 0; i < screens.count; i++) {
        const err = Ref();
        const ok = $.NSWorkspace.sharedWorkspace.setDesktopImageURLForScreenOptionsError(
            url, screens.objectAtIndex(i), $({}), err);
        if (!ok) throw new Error(err[0].localizedDescription.js);
    }
}
"""

# Imports files into a Photos album, creating it if needed. Photos is quit
# afterwards if it wasn't already open, so the nightly run leaves no window.
PHOTOS_IMPORT_APPLESCRIPT = """
on run argv
    set albumName to item 1 of argv
    set imageFiles to {}
    repeat with p in rest of argv
        set end of imageFiles to POSIX file (contents of p)
    end repeat
    set wasRunning to application "Photos" is running
    tell application "Photos"
        if not (exists album albumName) then make new album named albumName
        with timeout of 600 seconds
            import imageFiles into album albumName skip check duplicates true
        end timeout
    end tell
    if not wasRunning then tell application "Photos" to quit
end run
"""

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Image:
    date: str
    desc: str
    url: str

    def markdown(self) -> str:
        return f"{self.date} | [{self.desc}]({self.url})\n\n"

    def table_cell(self) -> str:
        thumb = f"{self.url}&pid=hp&w=384&h=216&rs=1&c=4"
        return f"![{self.desc}]({thumb}) {self.date} [download 4k]({self.url})"

    def large_url(self) -> str:
        return f"{self.url}&w=960"

    def portrait_url(self) -> str:
        """Bing's native 1080x1920 phone version — a separate vertical shot,
        not a crop of the 4K original."""
        return self.url.replace("_UHD.jpg", "_1080x1920.jpg")


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def _parse_entry(entry: dict) -> Image:
    url = BING_URL.format(entry["url"])
    url = url.split("&", 1)[0]  # bare URL, no w/h params -> full-quality 4K
    desc = entry["copyright"]
    enddate = entry["enddate"]  # e.g. "20260906"
    formatted_date = f"{enddate[0:4]}-{enddate[4:6]}-{enddate[6:8]}"
    return Image(formatted_date, desc, url)


def fetch_recent() -> list[Image]:
    """Request Bing's entire available window (today plus ~7 prior days).

    Bing's live API never exposes more than this, newest first.
    """
    resp = json.loads(http_get(BING_API))
    return [_parse_entry(entry) for entry in resp["images"]]


def read_wallpaper_list() -> list[Image]:
    """Parse bing-wallpaper.md back into Image objects."""
    if not WALLPAPER_LIST_PATH.exists():
        return []
    pattern = re.compile(r"(.*)\s\|.*\[(.*)\]\((.*)\)")
    images = []
    for line in WALLPAPER_LIST_PATH.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            images.append(Image(match.group(1), match.group(2), match.group(3)))
    return images


def dedupe(images: list[Image]) -> list[Image]:
    """Keep one record per date: the first seen, unless a later one is EN-US
    and the kept one isn't. Bing has served some days under a second market
    (ROW, ZH-CN), sometimes with a different picture; EN-US is the norm."""
    by_date: dict[str, Image] = {}
    for img in images:
        kept = by_date.get(img.date)
        if kept is None or ("_EN-US" in img.url and "_EN-US" not in kept.url):
            by_date[img.date] = img
    return list(by_date.values())


def filter_recent(images: list[Image], days: int) -> list[Image]:
    """Deduplicate and keep only records from the last `days` days."""
    cutoff = date.today() - timedelta(days=days)
    recent = []
    for img in dedupe(images):
        try:
            img_date = date.fromisoformat(img.date)
        except ValueError:
            continue
        if img_date >= cutoff:
            recent.append(img)
    return recent


def write_wallpaper_list(images: list[Image]) -> None:
    """Write bing-wallpaper.md (keeps the full history)."""
    lines = ["## Bing Wallpaper", "\n\n"]
    for img in dedupe(images):
        lines.append(img.markdown())
    WALLPAPER_LIST_PATH.write_text("".join(lines), encoding="utf-8")


def write_readme(images: list[Image]) -> None:
    """Write README.md — only the last RETENTION_DAYS days."""
    images = dedupe(images)
    lines = [
        "## Bing Wallpaper\n\n",
        f"Showing the last {RETENTION_DAYS} days. Updated manually via "
        "`python3 bing_wallpaper.py` (see [bing-wallpaper.md](bing-wallpaper.md) "
        "for full history). Full 4K (3840x2160) originals are cached locally in "
        "`wallpapers/`.\n\n",
    ]

    top = images[0]
    lines.append(f"![{top.desc}]({top.large_url()})\n")
    lines.append(f"Today: [{top.desc}]({top.url})\n")
    lines.append("|      |      |      |\n")
    lines.append("| :--: | :--: | :--: |\n")

    row = "|"
    for i, img in enumerate(images, start=1):
        row += img.table_cell() + "|"
        if i % 3 == 0:
            lines.append(row + "\n")
            row = "|"
    if row != "|":
        lines.append(row + "\n")

    README_PATH.write_text("".join(lines), encoding="utf-8")


def download_wallpaper(image: Image) -> None:
    """Download today's 4K original (3840x2160) into wallpapers/, named by date."""
    WALLPAPER_DIR.mkdir(exist_ok=True)
    target = WALLPAPER_DIR / f"{image.date}.jpg"
    if target.exists():
        return
    data = http_get(image.url)
    target.write_bytes(data)
    print(f"✅ Downloaded 4K wallpaper: {target.name} ({len(data)} bytes)")


def prune_wallpaper_dir(days: int) -> None:
    """Delete image files in wallpapers/ older than `days` days."""
    if not WALLPAPER_DIR.is_dir():
        return
    cutoff = date.today() - timedelta(days=days)
    for path in WALLPAPER_DIR.glob("*.jpg"):
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if file_date < cutoff:
            path.unlink()
            print(f"🗑️ Pruned old wallpaper: {path.name}")


def backfill(days: int) -> None:
    """Download every archived image from the last `days` days into wallpapers/.

    Bing's live API only exposes ~8 days of history, so this reads the URLs
    already recorded in bing-wallpaper.md instead of hitting the API. It
    runs `git pull` first so records committed from another machine are
    included.
    """
    print("Pulling latest records...")
    try:
        subprocess.run(["git", "pull"], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"⚠️ git pull failed ({e}); continuing with local records.")
    recent = filter_recent(read_wallpaper_list(), days)
    print(f"Backfilling {len(recent)} wallpaper(s) from the last {days} days...")
    for img in recent:
        download_wallpaper(img)
    prune_wallpaper_dir(days)


def set_desktop_wallpaper(path: Path) -> None:
    subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", SET_WALLPAPER_JXA, str(path.resolve())],
        check=True,
    )


def rotate() -> None:
    """Set the newest never-shown image in wallpapers/ as the desktop picture.

    Shown dates are appended to SHOWN_PATH, so each image is used once. When
    every cached image has been shown, the current wallpaper is left alone
    until a manual fetch adds new ones.
    """
    shown = set(SHOWN_PATH.read_text().split()) if SHOWN_PATH.exists() else set()
    unshown = sorted(
        (p for p in WALLPAPER_DIR.glob("*.jpg") if p.stem not in shown), reverse=True
    )
    if not unshown:
        print(
            "⚠️ Every cached wallpaper has been shown; keeping the current one. "
            "Run `python3 bing_wallpaper.py` to fetch new ones."
        )
        return
    pick = unshown[0]
    set_desktop_wallpaper(pick)
    with SHOWN_PATH.open("a") as f:
        f.write(pick.stem + "\n")
    print(f"🖼️ Desktop set to {pick.name} ({len(unshown) - 1} unshown left)")


def sync_photos(days: int) -> None:
    """Import the portrait version of each recent record into PHOTOS_ALBUM.

    Reads bing-wallpaper.md (like --backfill) rather than wallpapers/, since
    the phone wants the 1080x1920 version. Photos keeps its own copy, so the
    downloads only live in a temp dir. Photos' AppleScript can't remove
    items, so the album keeps growing; delete old ones in Photos by hand.
    """
    imported = (
        set(PHOTOS_IMPORTED_PATH.read_text().split())
        if PHOTOS_IMPORTED_PATH.exists()
        else set()
    )
    pending = [
        img for img in reversed(filter_recent(read_wallpaper_list(), days))
        if img.date not in imported
    ]
    if not pending:
        print("Photos album already up to date.")
        return
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for img in pending:
            path = Path(tmp) / f"{img.date}.jpg"
            path.write_bytes(http_get(img.portrait_url()))
            paths.append(str(path))
        subprocess.run(
            ["osascript", "-e", PHOTOS_IMPORT_APPLESCRIPT, PHOTOS_ALBUM, *paths],
            check=True,
        )
    with PHOTOS_IMPORTED_PATH.open("a") as f:
        f.writelines(img.date + "\n" for img in pending)
    print(f"📱 Imported {len(pending)} portrait wallpaper(s) into Photos album '{PHOTOS_ALBUM}'")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "Download all archived images from the last RETENTION_DAYS days "
            "into wallpapers/, instead of fetching a new 'today' image."
        ),
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help=(
            "Update bing-wallpaper.md and README.md from the Bing API as usual, "
            "but skip downloading/pruning wallpapers/ entirely."
        ),
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help=(
            "Set the newest not-yet-shown image in wallpapers/ as the desktop "
            "picture, without touching the archive or the network."
        ),
    )
    parser.add_argument(
        "--sync-photos",
        action="store_true",
        help=(
            f"Import the 1080x1920 portrait version of the last RETENTION_DAYS "
            f"days into the Photos album '{PHOTOS_ALBUM}' for the iPhone."
        ),
    )
    args = parser.parse_args()

    if args.sync_photos:
        sync_photos(RETENTION_DAYS)
        return

    if args.rotate:
        rotate()
        return

    if args.backfill:
        backfill(RETENTION_DAYS)
        return

    image_list = read_wallpaper_list()
    new_images = fetch_recent()  # newest first; self-heals gaps up to ~8 days
    image_list = new_images + image_list

    write_wallpaper_list(image_list)
    write_readme(filter_recent(image_list, RETENTION_DAYS))

    if args.no_download:
        return

    for img in new_images:
        download_wallpaper(img)
    prune_wallpaper_dir(RETENTION_DAYS)


if __name__ == "__main__":
    main()
