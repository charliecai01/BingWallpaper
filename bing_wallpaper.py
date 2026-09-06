#!/usr/bin/env python3
"""Fetch Bing's picture of the day and update the local archive.

Run manually whenever you want a fresh wallpaper — there is no scheduled job.
"""

import argparse
import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

README_PATH = Path("README.md")
WALLPAPER_LIST_PATH = Path("bing-wallpaper.md")
WALLPAPER_DIR = Path("wallpapers")

# Retention window: README.md and wallpapers/ only keep the last 30 days.
# bing-wallpaper.md is untouched by this and keeps the full history.
RETENTION_DAYS = 30

# n=8 pulls Bing's entire available history in one request (idx=0 is today).
# Bing never exposes more than ~8 days back, so fetching all of them every
# run means a gap of up to 8 days between runs still self-heals; skip more
# than that and the missed days are gone for good on Bing's end.
BING_API = (
    "https://cn.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8"
    "&nc=1618537156988&pid=hp&uhd=1&uhdwidth=3840&uhdheight=2160"
)
BING_URL = "https://cn.bing.com{}"

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
    seen = set()
    result = []
    for img in images:
        if img not in seen:
            seen.add(img)
            result.append(img)
    return result


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
    already recorded in bing-wallpaper.md instead of hitting the API.
    """
    recent = filter_recent(read_wallpaper_list(), days)
    print(f"Backfilling {len(recent)} wallpaper(s) from the last {days} days...")
    for img in recent:
        download_wallpaper(img)
    prune_wallpaper_dir(days)


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
    args = parser.parse_args()

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
