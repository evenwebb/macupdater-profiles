#!/usr/bin/env python3
"""
Batch icon extractor for macupdater-profiles.

Two modes:
  --installed    Extract icons from apps already in /Applications (fast)
  --download     Download the app from its profile's download URL,
                 extract the icon, and save it into the repo (slow but
                 works for apps you don't have installed).

Output: icons/{slug}.png in the repo root.
After extraction, the script updates each profile's icons.direct_url
to point at the raw GitHub URL.

Usage:
    python3 scripts/extract_icons.py --installed
    python3 scripts/extract_icons.py --installed --slug firefox
    python3 scripts/extract_icons.py --download --slug handbrake
    python3 scripts/extract_icons.py --download --slug handbrake --slug vlc

Prerequisites: macOS (uses sips, hdiutil).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ICONS_DIR = REPO / "icons"
MANIFEST_PATH = REPO / "manifest.json"

SKIP_DIRS = {".git", ".github", "scripts", "__pycache__"}


# ── core extraction ──────────────────────────────────────────────────

def extract_icon_from_app(app_path: str, slug: str) -> Path | None:
    """Extract a 256px PNG icon from a .app bundle. Returns path or None."""
    app = os.path.expanduser(app_path)
    if not os.path.isdir(app):
        return None

    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    out = ICONS_DIR / f"{slug}.png"
    tmp = ICONS_DIR / f"{slug}.tmp.png"

    # Try extracting directly from .app
    try:
        r = subprocess.run(
            ["sips", "-s", "format", "png", app, "--out", str(tmp),
             "-Z", "256"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and tmp.stat().st_size > 0:
            tmp.rename(out)
            return out
    except Exception:
        pass

    # Try .icns files inside the bundle
    icns_candidates = [
        os.path.join(app, "Contents", "Resources", "AppIcon.icns"),
        os.path.join(app, "Contents", "Resources", "document.icns"),
        os.path.join(app, "Contents", "Resources", "icon.icns"),
    ]
    for icns in icns_candidates:
        if not os.path.isfile(icns):
            continue
        try:
            r = subprocess.run(
                ["sips", "-s", "format", "png", icns, "--out", str(tmp),
                 "-Z", "256"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0 and tmp.stat().st_size > 0:
                tmp.rename(out)
                return out
        except Exception:
            pass

    return None


# ── installed-apps mode ──────────────────────────────────────────────

def extract_from_installed(slugs: list[str] | None = None) -> int:
    """Walk /Applications, match to profiles, extract icons."""
    apps_dir = "/Applications"
    if not os.path.isdir(apps_dir):
        print(f"ERROR: {apps_dir} not found — are you on macOS?", file=sys.stderr)
        return 1

    manifest = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}
    apps = manifest.get("apps", {})

    extracted = 0
    skipped = 0

    for entry in os.listdir(apps_dir):
        if not entry.endswith(".app"):
            continue
        app_path = os.path.join(apps_dir, entry)

        # Read bundle ID
        info_plist = os.path.join(app_path, "Contents", "Info.plist")
        if not os.path.isfile(info_plist):
            continue

        try:
            r = subprocess.run(
                ["defaults", "read", info_plist, "CFBundleIdentifier"],
                capture_output=True, text=True, timeout=5,
            )
            bundle_id = r.stdout.strip()
        except Exception:
            continue

        if not bundle_id:
            continue

        # Find matching profile
        slug = None
        for s, info in apps.items():
            if info.get("bundle_id") == bundle_id:
                slug = s
                break
            if bundle_id in info.get("alternate_bundle_ids", []):
                slug = s
                break

        if not slug:
            continue

        if slugs and slug not in slugs:
            continue

        # Already have icon?
        existing = ICONS_DIR / f"{slug}.png"
        if existing.exists():
            skipped += 1
            continue

        result = extract_icon_from_app(app_path, slug)
        if result:
            print(f"  ✅ {slug} ({entry})")
            update_profile_icon_url(slug, app_path, apps)
            extracted += 1
        else:
            print(f"  ⚠ {slug}: extraction failed")

    print(f"\nExtracted: {extracted}  Skipped (cached): {skipped}")
    return 0


# ── download mode ────────────────────────────────────────────────────

def extract_from_download(slugs: list[str]) -> int:
    """Download each app's DMG, extract icon, clean up."""
    manifest = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}
    apps = manifest.get("apps", {})

    extracted = 0
    for slug in slugs:
        info = apps.get(slug)
        if not info:
            print(f"  ⚠ {slug}: not in manifest")
            continue

        path_rel = info.get("path", "")
        profile_path = REPO / path_rel
        if not profile_path.exists():
            print(f"  ⚠ {slug}: profile file not found at {path_rel}")
            continue

        profile = json.loads(profile_path.read_text())
        dl = profile.get("download", {})
        dl_url = dl.get("url", "")

        if not dl_url:
            print(f"  ⚠ {slug}: no download URL in profile")
            continue

        print(f"  ⬇ {slug}: downloading from {dl_url[:80]}...")

        work = tempfile.mkdtemp(prefix="macupdater-icon-")
        mounts: list[str] = []
        try:
            # Download
            dest = os.path.join(work, os.path.basename(dl_url.split("?")[0]) or f"{slug}.dmg")
            urllib.request.urlretrieve(dl_url, dest)

            # Extract
            if dest.endswith((".dmg", ".iso")):
                r = subprocess.run(
                    ["hdiutil", "attach", dest, "-nobrowse", "-mountpoint",
                     os.path.join(work, "mnt")],
                    capture_output=True, text=True, timeout=30,
                )
                mount_point = os.path.join(work, "mnt")
                mounts.append(mount_point)
                apps_found = list(Path(mount_point).glob("*.app"))
            elif dest.endswith(".zip"):
                subprocess.run(["ditto", "-xk", dest, work], check=True)
                apps_found = list(Path(work).rglob("*.app"))
            elif dest.endswith((".pkg", ".mpkg")):
                print(f"    ⚠ PKG format — cannot extract icon from installer")
                continue
            else:
                print(f"    ⚠ Unknown format: {os.path.splitext(dest)[1]}")
                continue

            if not apps_found:
                print(f"    ⚠ No .app found in archive")
                continue

            src_app = str(apps_found[0])
            result = extract_icon_from_app(src_app, slug)
            if result:
                update_profile_icon_url(slug, src_app, apps)
                print(f"    ✅ {slug}")
                extracted += 1
            else:
                print(f"    ⚠ icon extraction failed")

        except Exception as e:
            print(f"    ❌ {slug}: {e}")
        finally:
            for mnt in reversed(mounts):
                subprocess.run(["hdiutil", "detach", mnt, "-quiet"], capture_output=True)
            shutil.rmtree(work, ignore_errors=True)

    print(f"\nExtracted: {extracted}/{len(slugs)}")
    return 0


# ── profile update ───────────────────────────────────────────────────

def update_profile_icon_url(slug: str, app_path: str, apps: dict) -> None:
    """Set icons.direct_url in the profile JSON to point at the repo-hosted PNG."""
    if slug not in apps:
        return

    entry = apps[slug]
    path_rel = entry.get("path", "")
    profile_path = REPO / path_rel
    if not profile_path.exists():
        return

    raw_url = (
        f"https://raw.githubusercontent.com/evenwebb/"
        f"macupdater-profiles/main/icons/{slug}.png"
    )

    profile = json.loads(profile_path.read_text())
    icons = profile.setdefault("icons", {})
    icons["direct_url"] = raw_url
    profile["icons"] = icons

    with open(profile_path, "w") as f:
        json.dump(profile, f, indent=2)
        f.write("\n")

    print(f"    ↳ icons.direct_url = {raw_url}")


# ── main ─────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Extract app icons for macupdater-profiles")
    p.add_argument("--installed", action="store_true",
                   help="Extract icons from apps in /Applications")
    p.add_argument("--download", action="store_true",
                   help="Download app DMG/ZIP, extract icon, clean up")
    p.add_argument("--slug", action="append", default=None, dest="slugs",
                   help="Only process these slugs (repeatable)")
    args = p.parse_args()

    if not args.installed and not args.download:
        p.error("Must specify --installed or --download")

    slugs = args.slugs or None

    if args.download:
        if not slugs:
            p.error("--download requires at least one --slug")
        return extract_from_download(slugs)

    return extract_from_installed(slugs)


if __name__ == "__main__":
    sys.exit(main())
