#!/usr/bin/env python3
"""Fetch Bing's picture of the day and update the local archive.

The GitHub Action (.github/workflows/update-archive.yml) runs this with
--no-download daily and commits the text files; run it manually for a fresh
wallpaper sooner. Locally, --backfill (daily via auto_backfill.sh) and
--rotate (at midnight and noon via auto_rotate.sh) run automatically.
"""

import argparse
import json
import plistlib
import random
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

README_PATH = Path("README.md")
WALLPAPER_LIST_PATH = Path("bing-wallpaper.md")
WALLPAPER_DIR = Path("wallpapers")
# Dates (file stems) of every wallpaper --rotate has put on the desktop in the
# current cycle, so none is shown twice. Cleared once every cached image has
# been shown. Local to this machine, like wallpapers/ itself.
SHOWN_PATH = Path(".shown_wallpapers")
# 1080x1920 portrait URLs of the last RETENTION_DAYS days, one per line, in a
# new random order on every write. The iPhone's "Bing Wallpaper" Shortcut reads
# it from GitHub, where the GitHub Action refreshes it daily, and sets the first
# URL it hasn't shown yet, so the shuffle is what makes its pick random.
PHONE_LIST_PATH = Path("phone-wallpapers.txt")
# --rotate sets a copy of the picked image with its location drawn in the
# lower-right corner, so the originals in wallpapers/ stay clean. Only the
# copy currently on the desktop is kept. Each copy is named after its date:
# WallpaperAgent keys on the file name (see read_store_with_image()).
CAPTION_DIR = Path(".captioned")
# Width/height of the built-in display, saved whenever --rotate sees it, so a
# caption drawn with the lid closed still fits once the lid opens.
BUILTIN_ASPECT_PATH = Path(".builtin_aspect")

# Retention window: wallpapers/ and phone-wallpapers.txt keep the last 90 days.
# bing-wallpaper.md is untouched by this and keeps the full history.
RETENTION_DAYS = 90
# README.md's gallery shows a shorter window than what is stored.
README_DAYS = 30

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

# macOS's wallpaper settings (undocumented). WallpaperAgent falls back to
# AllSpacesAndDisplays for any Space or display with no entry of its own, and
# reads the file when it starts.
WALLPAPER_STORE = (
    Path.home() / "Library/Application Support/com.apple.wallpaper/Store/Index.plist"
)
WALLPAPER_STORE_TIMEOUT = 10  # seconds to wait for WallpaperAgent to save

# Sets the desktop picture on every screen through NSWorkspace via the JXA
# ObjC bridge. Unlike `tell application "System Events"`, this sends no Apple
# Events, so it needs no Automation permission and works from launchd. It only
# covers the current Space, and clears "Show on all Spaces" as a side effect;
# set_desktop_wallpaper() undoes that.
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

# Draws argv[2] (one line per "\n") right-aligned in small, semi-transparent
# text, colored from the picture behind it, in the lower-right corner of the
# JPEG argv[0], writing the result to argv[1]. AppKit through JXA, like
# SET_WALLPAPER_JXA, so no image library is needed. The picture is first
# scaled up to cover the largest screen's pixels, so the text is drawn at
# native resolution and WallpaperAgent never enlarges (and so blurs) it.
CAPTION_JXA = """
ObjC.import('AppKit');
function run(argv) {
    const [src, dst, text, saved] = argv;
    const rep = $.NSBitmapImageRep.imageRepWithData($.NSData.dataWithContentsOfFile(src));
    if (rep.isNil()) throw new Error('cannot read ' + src);
    const w = rep.pixelsWide, h = rep.pixelsHigh;
    // Scale to cover the largest screen. The text is placed for the built-in
    // display, whose width/height argv[3] remembers for when the lid is
    // closed (with neither, the narrowest screen): the desktop fills each
    // screen, cropping the image's sides on screens narrower than it, so the
    // text is inset by that screen's crop. Returns the built-in display's
    // width/height if it is on, for the caller to save.
    let scale = 1, aspect = w / h, builtin = null;
    const screens = $.NSScreen.screens;
    for (let i = 0; i < screens.count; i++) {
        const screen = screens.objectAtIndex(i);
        const f = screen.frame.size, k = screen.backingScaleFactor;
        scale = Math.max(scale, f.width * k / w, f.height * k / h);
        aspect = Math.min(aspect, f.width / f.height);
        if (screen.localizedName.js.startsWith('Built-in')) builtin = f.width / f.height;
    }
    const target = builtin !== null ? builtin : parseFloat(saved);
    if (target > 0) aspect = Math.min(w / h, target);
    const W = Math.round(w * scale), H = Math.round(h * scale);
    // The screen shows the full height (it is never wider than the image), so
    // image pixels map to screen pixels at the same ratio both ways.
    const cropX = (W - H * aspect) / 2;
    const out = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
        null, W, H, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
    const ctx = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(out);
    $.NSGraphicsContext.saveGraphicsState;
    $.NSGraphicsContext.setCurrentContext(ctx);
    ctx.setImageInterpolation($.NSImageInterpolationHigh);
    rep.drawInRect($.NSMakeRect(0, 0, W, H));
    // Lay the lines out bottom-up before choosing a color. Same gap to the
    // right edge of the screen as from the bottom edge to the last line's
    // baseline; a line's origin is the bottom of its line box, below the
    // baseline by the font's descent.
    const font = $.NSFont.systemFontOfSizeWeight(Math.round(H * 0.011), $.NSFontWeightMedium);
    const fontOnly = $.NSDictionary.dictionaryWithObjectForKey(font, $.NSFontAttributeName);
    const margin = H * 0.02;
    const right = W - cropX - margin;
    const bottom = Math.round(margin + font.descender);
    const lines = [];
    let left = right, y = bottom;
    for (const line of text.split('\\n').reverse()) {
        const str = $.NSString.stringWithString(line);
        const size = str.sizeWithAttributes(fontOnly);
        const x = Math.round(right - size.width);
        lines.push([str, x, y]);
        left = Math.min(left, x);
        y += Math.round(size.height);
    }
    // Like the iOS lock-screen clock, take the color from what's behind the
    // text: a deep shade of it on a light background, a pale tint of it on a
    // dark one. Averages a grid of source pixels (rows count from the top).
    const srgb = $.NSColorSpace.sRGBColorSpace;
    let r = 0, g = 0, b = 0, n = 0;
    for (let i = 0; i < 32; i++) {
        for (let j = 0; j < 8; j++) {
            const px = Math.floor((left + (right - left) * (i + 0.5) / 32) / scale);
            const py = Math.floor(h - (bottom + (y - bottom) * (j + 0.5) / 8) / scale);
            const c = rep.colorAtXY(px, py).colorUsingColorSpace(srgb);
            r += c.redComponent; g += c.greenComponent; b += c.blueComponent; n++;
        }
    }
    const bg = $.NSColor.colorWithSRGBRedGreenBlueAlpha(r / n, g / n, b / n, 1);
    const light = (0.2126 * r + 0.7152 * g + 0.0722 * b) / n > 0.55;
    const hue = bg.hueComponent, sat = bg.saturationComponent;
    const color = light
        ? $.NSColor.colorWithHueSaturationBrightnessAlpha(hue, Math.min(0.7, sat * 1.5), 0.3, 0.8)
        : $.NSColor.colorWithHueSaturationBrightnessAlpha(hue, sat * 0.25, 1, 0.8);
    // A 1-pixel unblurred shadow in the opposite tone keeps the edges crisp.
    const shadow = $.NSShadow.alloc.init;
    shadow.shadowColor = $.NSColor.colorWithWhiteAlpha(light ? 1 : 0, light ? 0.25 : 0.45);
    shadow.shadowBlurRadius = 1;
    shadow.shadowOffset = $.NSMakeSize(0, -1);
    const attrs = $.NSDictionary.dictionaryWithObjectsForKeys(
        $([font, color, shadow]),
        $([$.NSFontAttributeName, $.NSForegroundColorAttributeName, $.NSShadowAttributeName]));
    for (const [str, x, ly] of lines) str.drawAtPointWithAttributes($.NSMakePoint(x, ly), attrs);
    $.NSGraphicsContext.restoreGraphicsState;
    const jpeg = out.representationUsingTypeProperties(
        $.NSBitmapImageFileTypeJPEG, $({NSImageCompressionFactor: 0.98}));
    if (!jpeg.writeToFileAtomically(dst, true)) throw new Error('cannot write ' + dst);
    return builtin === null ? '' : String(builtin);
}
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
    """Write README.md — only the last README_DAYS days."""
    images = dedupe(images)
    lines = [
        "## Bing Wallpaper\n\n",
        f"Showing the last {README_DAYS} days. Updated daily by GitHub Actions via "
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


def write_phone_list(images: list[Image]) -> None:
    """Write phone-wallpapers.txt, shuffled — only the last RETENTION_DAYS days."""
    lines = [img.portrait_url() + "\n" for img in dedupe(images)]
    random.shuffle(lines)
    PHONE_LIST_PATH.write_text("".join(lines), encoding="utf-8")


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


def desktop_entries(store: dict):
    """Yield every entry in the wallpaper store that can hold a desktop picture.

    Which one NSWorkspace updates depends on the store's current shape: the
    all-Spaces default while "Show on all Spaces" holds, otherwise the
    per-Space and per-display entries under Spaces.
    """
    yield store.get("SystemDefault", {})
    yield store.get("AllSpacesAndDisplays", {})
    yield from store.get("Displays", {}).values()
    for space in store.get("Spaces", {}).values():
        yield space.get("Default", {})
        yield from space.get("Displays", {}).values()


def read_store_with_image(path: Path, since: datetime) -> tuple[dict, dict]:
    """Return the wallpaper store, and the entry holding `path`, once
    WallpaperAgent has saved `path` to it at or after `since` (naive UTC).

    NSWorkspace returns before the agent writes the file. Checking the time,
    not just the name, keeps a re-set of the same file from matching the
    store's previous save.
    """
    deadline = time.monotonic() + WALLPAPER_STORE_TIMEOUT
    while True:
        store = plistlib.loads(WALLPAPER_STORE.read_bytes())
        for entry in desktop_entries(store):
            desktop = entry.get("Desktop")
            if (desktop and desktop["LastSet"] >= since
                    and path.name.encode() in desktop["Content"]["Choices"][0]["Configuration"]):
                return store, entry
        if time.monotonic() > deadline:
            raise TimeoutError(f"WallpaperAgent did not save {path.name}")
        time.sleep(0.2)


def show_on_all_spaces(path: Path, since: datetime) -> None:
    """Make the wallpaper NSWorkspace just set apply to every Space and display.

    The entry NSWorkspace updated becomes both the system default and the
    all-Spaces entry, and the per-Space and per-display overrides are
    dropped. The screen saver (Idle) is kept. WallpaperAgent is restarted to
    load the file; launchd relaunches it.
    """
    store, entry = read_store_with_image(path, since)
    system = store.setdefault("SystemDefault", {})
    idle = (store.get("AllSpacesAndDisplays", {}).get("Idle")
            or system.get("Idle") or entry.get("Idle"))
    chosen = {"Type": "individual", "Desktop": entry["Desktop"]}
    if idle:
        chosen["Idle"] = idle
    store["SystemDefault"] = {**system, **chosen}
    store["AllSpacesAndDisplays"] = chosen
    store["Spaces"] = {}
    store["Displays"] = {}
    WALLPAPER_STORE.write_bytes(plistlib.dumps(store, fmt=plistlib.FMT_BINARY))
    subprocess.run(["killall", "WallpaperAgent"])


def set_desktop_wallpaper(path: Path) -> None:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", SET_WALLPAPER_JXA, str(path.resolve())],
        check=True,
    )
    show_on_all_spaces(path, since)


def location(desc: str) -> str:
    """Strip the trailing "(© photographer/agency)" credit from a description."""
    return re.sub(r"\s*\(©[^)]*\)\s*$", "", desc)


def captioned_copy(path: Path) -> Path:
    """Return a copy of `path` with its location in the lower-right corner.

    The location comes from bing-wallpaper.md, broken onto a second line after
    its first comma. Falls back to `path` itself if the date isn't recorded or
    drawing fails, so rotation never stops over it.
    """
    desc = next((i.desc for i in read_wallpaper_list() if i.date == path.stem), None)
    if desc is None:
        print(f"⚠️ No description for {path.stem}; setting it without a caption.")
        return path
    CAPTION_DIR.mkdir(exist_ok=True)
    target = CAPTION_DIR / path.name
    saved = BUILTIN_ASPECT_PATH.read_text().strip() if BUILTIN_ASPECT_PATH.exists() else ""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", CAPTION_JXA,
             str(path.resolve()), str(target.resolve()),
             location(desc).replace(", ", "\n", 1), saved],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Captioning failed ({e.stderr.strip()}); setting it without a caption.")
        return path
    builtin = result.stdout.strip()
    if builtin and builtin != saved:
        BUILTIN_ASPECT_PATH.write_text(builtin + "\n")
    return target


def rotate() -> None:
    """Set a random never-shown image in wallpapers/ as the desktop picture.

    Shown dates are appended to SHOWN_PATH, so no image repeats until every
    cached one has been shown; then SHOWN_PATH is cleared and a new cycle
    starts, skipping the image currently on the desktop.
    """
    cached = list(WALLPAPER_DIR.glob("*.jpg"))
    if not cached:
        print("⚠️ No cached wallpapers. Run `python3 bing_wallpaper.py --backfill`.")
        return
    shown_list = SHOWN_PATH.read_text().split() if SHOWN_PATH.exists() else []
    unshown = [p for p in cached if p.stem not in shown_list]
    if not unshown:
        print(f"🔁 All {len(cached)} cached wallpapers shown; starting a new cycle.")
        SHOWN_PATH.unlink(missing_ok=True)
        current = shown_list[-1] if shown_list else None
        shown_list = []
        unshown = [p for p in cached if p.stem != current] or cached
    pick = random.choice(unshown)
    left = sum(p.stem not in shown_list for p in cached) - 1
    shown = captioned_copy(pick)
    set_desktop_wallpaper(shown)
    for old in CAPTION_DIR.glob("*.jpg"):
        if old != shown:
            old.unlink()
    with SHOWN_PATH.open("a") as f:
        f.write(pick.stem + "\n")
    print(f"🖼️ Desktop set to {pick.name} ({left} unshown left)")


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
            "Update bing-wallpaper.md, README.md and phone-wallpapers.txt from the "
            "Bing API as usual, "
            "but skip downloading/pruning wallpapers/ entirely."
        ),
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help=(
            "Set a random not-yet-shown image in wallpapers/ as the desktop "
            "picture, without touching the archive or the network."
        ),
    )
    args = parser.parse_args()

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
    write_readme(filter_recent(image_list, README_DAYS))
    write_phone_list(filter_recent(image_list, RETENTION_DAYS))

    if args.no_download:
        return

    for img in new_images:
        download_wallpaper(img)
    prune_wallpaper_dir(RETENTION_DAYS)


if __name__ == "__main__":
    main()
