#!/usr/bin/env python3
"""
Linux-compatible icon extractor for macupdater-profiles.

Downloads the app DMG/ZIP, extracts the .icns from the .app bundle,
converts to 256px PNG via Pillow, saves to icons/{slug}.png,
and updates the profile's icons.direct_url.

Requirements (Linux):
    apt install p7zip-full          # or: brew install p7zip
    pip install Pillow

Usage:
    python3 scripts/extract_icons_linux.py firefox
    python3 scripts/extract_icons_linux.py handbrake vlc spotify
    python3 scripts/extract_icons_linux.py --all      # every profile
    python3 scripts/extract_icons_linux.py --category browsers

Output: icons/{slug}.png in the repo root.
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

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow not installed.  pip install Pillow", file=sys.stderr)
    sys.exit(1)

REPO = Path(__file__).resolve().parent.parent
ICONS_DIR = REPO / "icons"
MANIFEST_PATH = REPO / "manifest.json"

USER_AGENT = "MacUpdater-IconExtractor/1.0"


# ── download ──────────────────────────────────────────────────────────

def _resolve_github_release(url: str) -> str | None:
    """
    If url looks like a GitHub releases/latest link, resolve via API
    to get the first DMG/ZIP asset download URL.
    """
    import re
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/releases/latest", url)
    if not m:
        return None
    owner, repo = m.groups()
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(api_url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github.v3+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        for asset in data.get("assets", []):
            name = asset.get("name", "").lower()
            if name.endswith((".dmg", ".zip")) and "mac" in name:
                return asset["browser_download_url"]
        # Fall back to first DMG/ZIP
        for asset in data.get("assets", []):
            name = asset.get("name", "").lower()
            if name.endswith((".dmg", ".zip")):
                return asset["browser_download_url"]
    except Exception:
        pass
    return None


def _scrape_download_links(html: str, base_url: str) -> list[str]:
    """Extract .dmg/.zip/.pkg download links from an HTML page."""
    import re
    from urllib.parse import urljoin
    links = []
    # Find href attributes pointing to binary files
    for m in re.finditer(r'href=["\']([^"\']+\.(?:dmg|zip|pkg|tar\.xz|tbz))["\']', html, re.I):
        href = m.group(1)
        full = urljoin(base_url, href)
        links.append(full)
    # Also try to find download buttons/links by text
    for m in re.finditer(r'href=["\']([^"\']+)["\'][^>]*>\s*(?:Download|Mac|macOS|\.dmg)', html, re.I):
        href = m.group(1)
        if not any(href.lower().endswith(ext) for ext in ('.dmg','.zip','.pkg','.tar.xz','.tbz')):
            full = urljoin(base_url, href)
            if full not in links:
                links.append(full)
    return links


def _resolve_url(url: str) -> tuple[str, str]:
    """
    Follow redirects to get the final URL and content-type.
    Resolves GitHub release pages, scrapes download links from HTML pages,
    and follows HTTP redirects.
    Returns (final_url, content_type).
    """
    # Try GitHub API first
    gh_url = _resolve_github_release(url)
    if gh_url:
        return gh_url, "application/octet-stream"

    # Try HEAD to check for redirects
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        final_url = resp.geturl()
        ct = resp.headers.get("Content-Type", "")
        if "text/html" not in ct:
            return final_url, ct
    except Exception:
        pass

    # Page is HTML — try GET and scrape for download links
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        resp = urllib.request.urlopen(req, timeout=30)
        html = resp.read().decode("utf-8", errors="replace")
        final_url = resp.geturl()

        links = _scrape_download_links(html, final_url)
        if links:
            # Prefer .dmg links, then .zip
            for link in links:
                if link.lower().endswith(".dmg"):
                    return link, "application/x-apple-diskimage"
            for link in links:
                if link.lower().endswith(".zip"):
                    return link, "application/zip"
            return links[0], "application/octet-stream"
    except Exception:
        pass

    return url, "text/html"


def download(url: str, work_dir: str, slug: str) -> tuple[str, str] | None:
    """
    Download a file, following redirects. Determines extension from final URL
    or Content-Type. Returns (dest_path, ext) or None on failure.
    """
    # Resolve redirects first
    final_url, content_type = _resolve_url(url)

    # Skip HTML pages (not direct file URLs)
    if "text/html" in content_type:
        print(f"    ⚠ URL redirects to HTML page, not a file: {final_url[:80]}")
        return None

    # Determine extension
    url_path = final_url.split("?")[0]
    ext = os.path.splitext(url_path)[1].lower()
    if not ext:
        # Guess from Content-Type
        ct_map = {
            "application/x-apple-diskimage": ".dmg",
            "application/zip": ".zip",
            "application/octet-stream": ".dmg",
            "application/x-bzip2": ".bz2",
            "application/gzip": ".gz",
        }
        ext = ct_map.get(content_type.split(";")[0], ".dmg")

    dest = os.path.join(work_dir, f"{slug}{ext}")
    req = urllib.request.Request(final_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)

    actual_size = os.path.getsize(dest)
    if actual_size < 1024:
        print(f"    ⚠ Downloaded file too small ({actual_size} bytes)")
        return None
    return dest, ext


# ── extraction ───────────────────────────────────────────────────────

def _find_app_bundle(root: str) -> str | None:
    """Find the first .app bundle under root."""
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            if d.endswith(".app"):
                return os.path.join(dirpath, d)
    return None


def _find_icns(app_path: str) -> str | None:
    """Find the best .icns file in a .app bundle."""
    candidates = [
        os.path.join(app_path, "Contents", "Resources", "AppIcon.icns"),
        os.path.join(app_path, "Contents", "Resources", "document.icns"),
        os.path.join(app_path, "Contents", "Resources", "icon.icns"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Fallback: any .icns in Resources
    res = os.path.join(app_path, "Contents", "Resources")
    if os.path.isdir(res):
        for f in os.listdir(res):
            if f.endswith(".icns"):
                return os.path.join(res, f)
    return None


def extract_dmg(dmg_path: str, work_dir: str) -> str | None:
    """
    Extract a .dmg file with 7z.
    DMG contains a HFS partition which 7z can unpack in two passes.
    """
    # Pass 1: extract the DMG envelope
    stage1 = os.path.join(work_dir, "stage1")
    os.makedirs(stage1, exist_ok=True)
    r = subprocess.run(
        ["7z", "x", "-y", f"-o{stage1}", dmg_path],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        # Try single-pass — some DMGs don't have nested partitions
        app = _find_app_bundle(stage1)
        if app:
            return app

    # Pass 2: look for .hfs or .hfsx partition files
    for root, _, files in os.walk(stage1):
        for f in files:
            if f.endswith((".hfs", ".hfsx", ".dmg")):
                inner = os.path.join(root, f)
                stage2 = os.path.join(work_dir, "stage2")
                os.makedirs(stage2, exist_ok=True)
                subprocess.run(
                    ["7z", "x", "-y", f"-o{stage2}", inner],
                    capture_output=True, text=True, timeout=60,
                )
                app = _find_app_bundle(stage2)
                if app:
                    return app

    # Last resort: check stage1 for .app
    return _find_app_bundle(stage1)


def extract_zip(zip_path: str, work_dir: str) -> str | None:
    """Extract a .zip with 7z."""
    subprocess.run(
        ["7z", "x", "-y", f"-o{work_dir}", zip_path],
        capture_output=True, text=True, timeout=60,
    )
    return _find_app_bundle(work_dir)


def extract_pkg(pkg_path: str, work_dir: str) -> str | None:
    """Attempt to extract .pkg (flat package with 7z)."""
    os.makedirs(work_dir, exist_ok=True)
    subprocess.run(
        ["7z", "x", "-y", f"-o{work_dir}", pkg_path],
        capture_output=True, text=True, timeout=60,
    )
    # PKGs often have a Payload file — try extracting that too
    for root, _, files in os.walk(work_dir):
        for f in files:
            if f.lower() == "payload":
                payload_dir = os.path.join(work_dir, "payload_out")
                os.makedirs(payload_dir, exist_ok=True)
                subprocess.run(
                    ["7z", "x", "-y", f"-o{payload_dir}", os.path.join(root, f)],
                    capture_output=True, text=True, timeout=60,
                )
                app = _find_app_bundle(payload_dir)
                if app:
                    return app
    return _find_app_bundle(work_dir)


# ── icon conversion ───────────────────────────────────────────────────

def icns_to_png(icns_path: str, slug: str) -> Path | None:
    """Convert .icns to 256px PNG, save to icons/{slug}.png."""
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    out = ICONS_DIR / f"{slug}.png"
    try:
        img = Image.open(icns_path)
        # ICNS files contain multiple sizes — Pillow picks the largest
        if max(img.size) > 512:
            img.thumbnail((256, 256), Image.LANCZOS)
        img.save(str(out), "PNG")
        if out.stat().st_size > 0:
            return out
    except Exception as e:
        print(f"    ⚠ PIL error: {e}", file=sys.stderr)
    return None


# ── profile update ───────────────────────────────────────────────────

def update_profile_icon(slug: str) -> None:
    """Set icons.direct_url in the profile JSON."""
    manifest = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}
    entry = manifest.get("apps", {}).get(slug)
    if not entry:
        return

    profile_path = REPO / entry.get("path", "")
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


# ── audit ─────────────────────────────────────────────────────────────

def _read_plist(app_path: str) -> dict | None:
    """Read Info.plist from a .app bundle and return key fields."""
    plist_path = os.path.join(app_path, "Contents", "Info.plist")
    if not os.path.isfile(plist_path):
        return None

    # Try plistlib first (binary plist reader)
    import plistlib
    try:
        with open(plist_path, "rb") as f:
            return plistlib.load(f)
    except Exception:
        pass

    # Fall back to parsing XML plist as plain text
    try:
        import re
        text = open(plist_path, "rb").read().decode("utf-8", errors="replace")
        result = {}
        for key, pattern in [
            ("CFBundleIdentifier", r"<key>CFBundleIdentifier</key>\s*<string>([^<]+)</string>"),
            ("CFBundleShortVersionString", r"<key>CFBundleShortVersionString</key>\s*<string>([^<]+)</string>"),
            ("CFBundleVersion", r"<key>CFBundleVersion</key>\s*<string>([^<]+)</string>"),
            ("LSMinimumSystemVersion", r"<key>LSMinimumSystemVersion</key>\s*<string>([^<]+)</string>"),
        ]:
            m = re.search(pattern, text)
            if m:
                result[key] = m.group(1)
        return result if result else None
    except Exception:
        return None


def _check_architecture(app_path: str) -> str | None:
    """Run 'file' on the main binary to determine architecture."""
    info_plist = os.path.join(app_path, "Contents", "Info.plist")
    exec_name = os.path.basename(app_path).replace(".app", "")
    if os.path.isfile(info_plist):
        try:
            import re
            text = open(info_plist, "rb").read().decode("utf-8", errors="replace")
            m = re.search(r"<key>CFBundleExecutable</key>\s*<string>([^<]+)</string>", text)
            if m:
                exec_name = m.group(1)
        except Exception:
            pass

    binary = os.path.join(app_path, "Contents", "MacOS", exec_name)
    if not os.path.isfile(binary):
        return None

    try:
        r = subprocess.run(["file", binary], capture_output=True, text=True, timeout=10)
        out = r.stdout
        if "arm64" in out and "x86_64" in out:
            return "universal"
        elif "arm64" in out:
            return "arm64"
        elif "x86_64" in out:
            return "x86_64"
    except Exception:
        pass
    return None


def audit_app_bundle(app_path: str, slug: str, profile: dict) -> None:
    """
    Compare extracted .app metadata against the profile and fix discrepancies.
    Updates the profile JSON in place if corrections are needed.
    """
    plist = _read_plist(app_path)
    if not plist:
        return

    arch = _check_architecture(app_path)
    fixes = {}

    # Check bundle_id
    actual_bid = plist.get("CFBundleIdentifier")
    profile_bid = profile.get("bundle_id")
    if actual_bid and profile_bid and actual_bid != profile_bid:
        print(f"    🔧 bundle_id mismatch: profile={profile_bid} actual={actual_bid}")
        fixes["bundle_id"] = actual_bid

    # Check min_os
    actual_min = plist.get("LSMinimumSystemVersion")
    profile_min = (profile.get("min_os") or "").replace("macOS ", "").strip()
    if actual_min and profile_min:
        try:
            actual_parts = tuple(int(x) for x in actual_min.split("."))
            profile_parts = tuple(int(x) for x in profile_min.split("."))
            if actual_parts != profile_parts:
                print(f"    🔧 min_os mismatch: profile={profile_min} actual={actual_min}")
                fixes["min_os"] = f"macOS {actual_min}"
        except (ValueError, TypeError):
            pass

    # Check architecture
    profile_arch = profile.get("architecture", "universal")
    if arch and arch != profile_arch:
        # Only fix if profile says universal but actual is specific (common mistake)
        if profile_arch == "universal" and arch in ("arm64", "x86_64"):
            print(f"    🔧 architecture mismatch: profile={profile_arch} actual={arch}")
            fixes["architecture"] = arch

    # ── build number (future-proofing for Sparkle version matching) ──
    build = plist.get("CFBundleVersion")
    if build:
        profile["cf_bundle_version"] = build
        fixes["cf_bundle_version"] = build

    # ── display name ──
    display_name = plist.get("CFBundleDisplayName")
    if display_name and display_name != profile.get("name", ""):
        profile["bundle_display_name"] = display_name

    # ── Electron detection ──
    asar = os.path.join(app_path, "Contents", "Resources", "app.asar")
    if os.path.isfile(asar):
        profile["uses_electron"] = True
        fixes["uses_electron"] = True

    # ── Sparkle detection ──
    sparkle = os.path.join(app_path, "Contents", "Frameworks", "Sparkle.framework")
    if os.path.isdir(sparkle):
        profile["uses_sparkle"] = True

    if fixes:
        manifest = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}
        entry = manifest.get("apps", {}).get(slug)
        if entry:
            profile_path = REPO / entry.get("path", "")
            if profile_path.exists():
                updated = json.loads(profile_path.read_text())
                updated.update(fixes)
                with open(profile_path, "w") as f:
                    json.dump(updated, f, indent=2)
                    f.write("\n")
                fix_list = ", ".join(k for k in fixes if k not in ("cf_bundle_version", "bundle_display_name"))
                shown = fix_list or "metadata enriched"
                print(f"    ✅ {len(fixes)} fields added/corrected: {shown}")


# ── main logic ────────────────────────────────────────────────────────

def process_slug(slug: str) -> bool:
    """Download + extract icon for one profile. Returns True on success."""
    manifest = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}
    entry = manifest.get("apps", {}).get(slug)
    if not entry:
        print(f"  ⚠ {slug}: not in manifest")
        return False

    if entry.get("skip"):
        print(f"  ⚠ {slug}: marked skip=true")
        return False

    # Already have icon?
    existing = ICONS_DIR / f"{slug}.png"
    if existing.exists():
        print(f"  ✓ {slug}: already cached")
        return True

    profile_path = REPO / entry.get("path", "")
    if not profile_path.exists():
        print(f"  ⚠ {slug}: profile file not found")
        return False

    profile = json.loads(profile_path.read_text())
    dl = profile.get("download", {})
    dl_url = dl.get("url", "")

    if not dl_url:
        print(f"  ⚠ {slug}: no download URL")
        return False

    # Skip template URLs
    if "{" in dl_url:
        print(f"  ⚠ {slug}: template URL (contains {{variable}}) — cannot download")
        return False

    name = profile.get("name", slug)
    print(f"  ⬇ {slug}: {name}")

    work = tempfile.mkdtemp(prefix="macupdater-icon-")
    try:
        # Download with redirect resolution
        print(f"    resolving {dl_url[:90]}...")
        result = download(dl_url, work, slug)
        if not result:
            return False
        dest, ext = result
        print(f"    downloaded ({os.path.getsize(dest) // 1024} KB)")

        # Extract based on type
        if ext in (".dmg", ".iso"):
            print(f"    extracting DMG...")
            app_path = extract_dmg(dest, work)
        elif ext == ".zip":
            print(f"    extracting ZIP...")
            app_path = extract_zip(dest, work)
        elif ext in (".pkg", ".mpkg"):
            print(f"    extracting PKG...")
            app_path = extract_pkg(dest, work)
        else:
            print(f"    ⚠ unknown format: {ext}")
            return False

        if not app_path:
            print(f"    ⚠ no .app bundle found in archive")
            return False

        # Find .icns
        icns_path = _find_icns(app_path)
        if not icns_path:
            print(f"    ⚠ no .icns file found in {os.path.basename(app_path)}")
            return False

        # Audit the extracted app bundle against profile data
        audit_app_bundle(app_path, slug, profile)

        # Convert
        result = icns_to_png(icns_path, slug)
        if result:
            update_profile_icon(slug)
            size_kb = result.stat().st_size // 1024
            print(f"    ✅ icons/{slug}.png ({size_kb} KB)")
            return True
        else:
            print(f"    ❌ conversion failed")
            return False

    except Exception as e:
        print(f"    ❌ {e}")
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    p = argparse.ArgumentParser(
        description="Linux-compatible icon extractor for MacUpdater profiles"
    )
    p.add_argument("slugs", nargs="*", help="Profile slugs to process")
    p.add_argument("--all", action="store_true", help="Process all profiles")
    p.add_argument("--category", default=None, help="Process all profiles in a category")
    args = p.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}
    apps = manifest.get("apps", {})

    if args.all:
        slugs = sorted(apps.keys())
    elif args.category:
        slugs = sorted(
            s for s, e in apps.items()
            if e.get("category") == args.category and not e.get("skip")
        )
    elif args.slugs:
        slugs = args.slugs
    else:
        p.print_help()
        return 1

    if not slugs:
        print("No profiles to process.")
        return 1

    print(f"Processing {len(slugs)} profile(s)...\n")
    ok = 0
    for slug in slugs:
        if process_slug(slug):
            ok += 1

    print(f"\nDone: {ok}/{len(slugs)} icons extracted")
    return 0 if ok == len(slugs) else 1


if __name__ == "__main__":
    sys.exit(main())
