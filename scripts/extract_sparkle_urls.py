#!/usr/bin/env python3
"""
Download apps and extract their Sparkle SUFeedURL from Info.plist.
Used to fix profiles where the appcast URL is only known inside the app bundle.

Usage:
    python3 scripts/extract_sparkle_urls.py --slug arc              # Single app
    python3 scripts/extract_sparkle_urls.py --broken                # All broken/placeholder profiles
    python3 scripts/extract_sparkle_urls.py --slug arc --apply      # Extract and update profile
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

REPO = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ---------------------------------------------------------------------------
# DMG mounting (macOS only) vs ZIP fallback
# ---------------------------------------------------------------------------
IS_MACOS = sys.platform == "darwin"


def download(url: str, dest: Path) -> bool:
    """Download a file. Returns True on success."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        resp = urllib.request.urlopen(req, timeout=60)
        dest.write_bytes(resp.read())
        return True
    except Exception as e:
        print(f"  Download failed: {e}")
        return False


def extract_dmg(dmg_path: Path, dest_dir: Path) -> Path | None:
    """Extract a DMG and return path to .app or None. Works on macOS and Linux (via 7z)."""
    # Try hdiutil first (macOS)
    if IS_MACOS:
        mount_point = Path("/Volumes") / dmg_path.stem
        subprocess.run(["hdiutil", "detach", str(mount_point)], capture_output=True)
        result = subprocess.run(
            ["hdiutil", "attach", str(dmg_path), "-nobrowse", "-readonly", "-mountpoint", str(mount_point)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            try:
                apps = list(mount_point.glob("*.app"))
                if apps:
                    dest = dest_dir / apps[0].name
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(apps[0], dest)
                    return dest
                print("  No .app found in mounted DMG")
                return None
            finally:
                subprocess.run(["hdiutil", "detach", str(mount_point)], capture_output=True)
        print(f"  hdiutil mount failed: {result.stderr[:100]}")

    # Linux: use 7z to extract DMG contents
    result = subprocess.run(
        ["7z", "x", f"-o{dest_dir}", str(dmg_path), "-y"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"  7z extract failed: {result.stderr[:200]}")
        return None

    # 7z extracts DMG contents — look for .app recursively
    apps = list(dest_dir.glob("**/*.app"))
    if apps:
        return apps[0]

    # Sometimes 7z extracts a HFS+ image that needs a second extraction
    hfs_files = list(dest_dir.glob("*.hfs")) + list(dest_dir.glob("*.hfsx"))
    if hfs_files:
        print(f"  Found HFS image, extracting...")
        hfs_dir = dest_dir / "hfs_contents"
        hfs_dir.mkdir(exist_ok=True)
        result2 = subprocess.run(
            ["7z", "x", f"-o{hfs_dir}", str(hfs_files[0]), "-y"],
            capture_output=True, text=True, timeout=60
        )
        apps = list(hfs_dir.glob("**/*.app"))
        if apps:
            return apps[0]

    print("  No .app found after extraction")
    return None


def extract_zip(zip_path: Path, dest_dir: Path) -> Path | None:
    """Extract a ZIP and return path to .app or None."""
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(dest_dir)

    apps = list(dest_dir.glob("**/*.app"))
    return apps[0] if apps else None


def read_sufeed_url(app_path: Path) -> str | None:
    """Read SUFeedURL from an app's Info.plist or binary."""
    # Method 1: Check Info.plist
    plist_path = app_path / "Contents" / "Info.plist"
    if plist_path.exists():
        with open(plist_path, 'rb') as f:
            plist = plistlib.load(f)

        # Direct SUFeedURL
        url = plist.get("SUFeedURL")
        if url:
            return url

        # Check Sparkle framework plist
        sparkle_plist = app_path / "Contents" / "Frameworks" / "Sparkle.framework" / "Resources" / "Info.plist"
        if sparkle_plist.exists():
            with open(sparkle_plist, 'rb') as f:
                sp = plistlib.load(f)
            url = sp.get("SUFeedURL")
            if url:
                return url

    # Method 2: Search the app binary for URL strings
    bin_name = app_path.name.replace(".app", "")
    binary = app_path / "Contents" / "MacOS" / bin_name
    if not binary.exists():
        # Try to find any binary
        bins = list(app_path.glob("Contents/MacOS/*"))
        if bins:
            binary = bins[0]

    if binary.exists() and binary.is_file():
        result = subprocess.run(
            ["strings", str(binary)], capture_output=True, text=True, timeout=30
        )
        urls = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if any(w in line.lower() for w in ['appcast', 'sufeed', 'update.xml', 'sparkle/update']):
                if line.startswith("https://") and len(line) > 30:
                    urls.add(line)
        if urls:
            return sorted(urls, key=len)[0]  # Shortest URL is usually the feed

    return None


# ---------------------------------------------------------------------------
# Find download URL for a profile
# ---------------------------------------------------------------------------
def find_download_url(slug: str) -> str | None:
    """Try to find a download URL for an app from common sources."""
    # Known download pages for common apps
    KNOWN = {
        "acorn": "https://flyingmeat.com/acorn/",
        "bartender": "https://www.macbartender.com/",
        "bettertouchtool": "https://folivora.com/",
        "daisydisk": "https://daisydiskapp.com/",
        "fantastical": "https://flexibits.com/fantastical/",
        "hazel": "https://www.noodlesoft.com/",
        "la-texit": "https://www.chachatelier.fr/latexit/",
        "poedit": "https://poedit.net/download",
        "shottr": "https://shottr.cc/",
        "windsurf": "https://codeium.com/windsurf/download",
        "tableplus": "https://tableplus.com/download",
        "postman": "https://www.postman.com/downloads/",
        "tower": "https://www.git-tower.com/mac",
        "spark-mail": "https://sparkmailapp.com/",
        "arc": "https://arc.net/",
        "raycast": "https://raycast.com/",
        "linear": "https://linear.app/download",
    }
    return KNOWN.get(slug)


# Known-good direct download URLs for apps we need
KNOWN_DOWNLOADS = {
    "arc": "https://releases.arc.net/release/Arc-latest.dmg",
    "postman": "https://dl.pstmn.io/download/latest/osx_arm64",
    "tableplus": "https://tableplus.com/release/macos/tableplus_latest",
    "spark-mail": "https://sparkmailapp.com/download",
    "linear": "https://linear.app/download/mac",
    "tower": "https://www.git-tower.com/public/download/mac",
    "evoto": "https://www.evoto.ai/download",
    "one-switch": "https://fireball.studio/oneswitch/OneSwitch-latest.dmg",
    "acorn": "https://flyingmeat.com/download/latest/Acorn.zip",
    "bartender": "https://www.macbartender.com/Bartender6/Bartender6.dmg",
    "bettertouchtool": "https://folivora.com/releases/BetterTouchTool.zip",
    "daisydisk": "https://daisydiskapp.com/downloads/DaisyDisk.dmg",
    "fantastical": "https://flexibits.com/fantastical/download",
    "hazel": "https://www.noodlesoft.com/download/Hazel-latest.dmg",
    "la-texit": "https://www.chachatelier.fr/latexit/downloads/LaTeXiT.dmg",
    "poedit": "https://poedit.net/download/Poedit-latest.zip",
    "shottr": "https://shottr.cc/download/Shottr-latest.dmg",
    "windsurf": "https://codeium.com/windsurf/download/mac",
    "raycast": "https://releases.raycast.com/releases/latest/download",
}


def scrape_download_link(page_url: str, slug: str = "") -> str | None:
    """Scrape a download page for a DMG/ZIP link."""
    # Use known-good URL if available
    if slug in KNOWN_DOWNLOADS:
        return KNOWN_DOWNLOADS[slug]

    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": UA})
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read().decode("utf-8", "replace")

        # Look for DMG/ZIP download links — prefer direct file links
        patterns = [
            r'href="(https?://[^"]+\.dmg)"',
            r'href="(https?://[^"]+\.zip)"',
            r'href="(https?://[^"]+/download[^"]*)"',
        ]
        for p in patterns:
            matches = re.findall(p, body, re.IGNORECASE)
            if matches:
                return matches[0]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def process_profile(slug: str, apply: bool = False) -> str | None:
    """Process one profile: download app, extract SUFeedURL, optionally update."""
    profile = None
    for d in REPO.iterdir():
        if not d.is_dir() or d.name.startswith('.'): continue
        pf = d / f"{slug}.json"
        if pf.exists():
            profile = pf
            break

    if not profile:
        print(f"{slug}: profile not found")
        return None

    data = json.loads(profile.read_text())
    name = data.get("name", slug)
    method = data.get("version_check", {}).get("method", "")
    current_url = data.get("version_check", {}).get("url", "")

    # Only process sparkle profiles or ones with placeholder URLs
    if method not in ("sparkle_appcast", "sparkle_rss", "sparkle_xml", ""):
        if not current_url or current_url.startswith(("http://", "https://")):
            print(f"{slug}: skipping (method={method}, has URL)")
            return None

    print(f"\n=== {name} ({slug}) ===")

    # Find download page
    page_url = find_download_url(slug)
    if not page_url:
        print(f"  No known download page")
        return None

    print(f"  Page: {page_url}")

    # Scrape for download link
    dl_url = scrape_download_link(page_url, slug)
    if not dl_url:
        print(f"  Could not find download link on page")
        return None

    print(f"  Download: {dl_url}")

    # Download and extract
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dl_path = tmp / "download"

        # Determine file type from URL
        is_dmg = dl_url.lower().endswith(".dmg")
        is_zip = dl_url.lower().endswith(".zip")

        if not (is_dmg or is_zip):
            print(f"  Unknown file type: {dl_url}")
            return None

        if not download(dl_url, dl_path):
            return None

        # Extract
        app_path = None
        if is_dmg:
            app_path = extract_dmg(dl_path, tmp)
        elif is_zip:
            app_path = extract_zip(dl_path, tmp)

        if not app_path:
            print(f"  Could not extract .app")
            return None

        print(f"  App: {app_path.name}")

        # Read SUFeedURL
        sufeed = read_sufeed_url(app_path)
        if not sufeed:
            print(f"  No SUFeedURL found in Info.plist")
            return None

        print(f"  SUFeedURL: {sufeed}")

        if apply:
            data["version_check"]["url"] = sufeed
            if not data["version_check"].get("method"):
                data["version_check"]["method"] = "sparkle_appcast"
            profile.write_text(json.dumps(data, indent=2) + "\n")
            print(f"  ✓ Profile updated")
        else:
            print(f"  (dry run — use --apply to update)")

        return sufeed


def main():
    p = argparse.ArgumentParser(description="Extract Sparkle URLs from app bundles")
    p.add_argument("--slug", default="", help="Single profile slug")
    p.add_argument("--broken", action="store_true", help="Process all broken/placeholder profiles")
    p.add_argument("--apply", action="store_true", help="Update profiles with found URLs")
    args = p.parse_args()

    if args.slug:
        slugs = [args.slug]
    elif args.broken:
        # Find profiles with placeholder URLs or 404s
        slugs = []
        for d in sorted(REPO.iterdir()):
            if not d.is_dir() or d.name.startswith('.'): continue
            for pf in sorted(d.glob("*.json")):
                data = json.loads(pf.read_text())
                url = data.get("version_check", {}).get("url", "")
                method = data.get("version_check", {}).get("method", "")
                # Placeholder or sparkle with no real URL
                if not url.startswith(("http://", "https://")):
                    slugs.append(pf.stem)
        print(f"Found {len(slugs)} profiles with placeholder URLs")
    else:
        print("Specify --slug or --broken")
        return 1

    if not IS_MACOS:
        print("Warning: DMG extraction requires macOS. Only ZIP downloads will work.")

    found = 0
    for slug in slugs:
        result = process_profile(slug, apply=args.apply)
        if result:
            found += 1

    print(f"\nDone. Found SUFeedURL for {found}/{len(slugs)} profiles.")
    if not args.apply:
        print("Re-run with --apply to update the profile files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
