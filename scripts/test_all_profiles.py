#!/usr/bin/env python3
"""
Full profile test suite — validates JSON, checks remote URLs, categorises failures.

Usage:
    python3 scripts/test_all_profiles.py              # Test everything
    python3 scripts/test_all_profiles.py --sample 20  # Random 20
    python3 scripts/test_all_profiles.py --report     # Markdown report
    python3 scripts/test_all_profiles.py --summary    # Summary only
    python3 scripts/test_all_profiles.py --badge      # Shields.io JSON

Exit code: number of genuinely broken profiles.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO / "manifest.json"
TIMEOUT = 15
WORKERS = 8
RETRIES = 2
UA = "MacUpdater-HealthCheck/3.0"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")

# ---------------------------------------------------------------------------
# Result categories
# ---------------------------------------------------------------------------
# BROKEN  = profile needs fixing (bad URL, broken XML, no version found)
# DEGRADED = works but has issues (redirect, missing optional fields)
# HEALTHY = confirmed working
# SKIPPED = can't test remotely (iTunes, MS autoupdate, etc.)
# TRANSIENT = likely temporary (connection error, timeout, server error)
# UNTESTABLE = no URL or can't parse (should be reviewed)

# ---------------------------------------------------------------------------
# Method support
# ---------------------------------------------------------------------------
SKIP_METHODS = {
    "itunes_api", "git_self_update", "none", "None", "",
    "microsoft_autoupdate", "broadcom_portal_only", "software_update_only",
    "download_and_parse_plist",
}

METHOD_ALIASES = {
    "electron_updater_yaml": "yaml_api", "rest_api": "json_api",
    "sparkle_xml": "sparkle_appcast", "sparkle_rss": "sparkle_appcast",
    "custom_xml_api": "sparkle_appcast", "github_api": "github_api",
    "json_api": "json_api", "yaml_api": "yaml_api",
    "scrape_html": "scrape_html", "redirect_trace": "redirect_trace",
    "plain_text_api": "plain_text_api", "sourceforge_json": "json_api",
}


def normalize_method(m: str) -> str:
    return METHOD_ALIASES.get(m, m)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------
def _normalize_extraction(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("regex:"):
            return {"pattern": s[len("regex:"):]}
        if s.startswith("sparkle:"):
            return {}
        if s in ("tag_name", "product", "version"):
            return {"json_path": s}
        return {"pattern": s}
    return {}


def fetch(url: str) -> tuple[int, str]:
    """Returns (status_code, body)."""
    if not url.startswith(("http://", "https://")):
        return -1, f"Invalid URL: {url[:80]}"
    headers = {"User-Agent": UA}
    if "api.github.com" in url and GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return -1, str(e)


def fetch_with_retry(url: str) -> tuple[int, str]:
    """Fetch with retries for transient errors."""
    for attempt in range(RETRIES + 1):
        code, body = fetch(url)
        if code != -1 or attempt == RETRIES:
            return code, body
        time.sleep(2 * (attempt + 1))
    return -1, "All retries failed"


# --- Sparkle ---
def _extract_sparkle(body: str) -> tuple[str | None, str | None]:
    ns = {"sparkle": "http://www.andymatuschak.org/xml-namespaces/sparkle"}
    root = ET.fromstring(body)
    items = root.findall("channel/item")
    if not items:
        return None, "No items in feed"

    best_ver, best = None, None
    for item in items:
        encl = item.find("enclosure")
        sv = item.findtext("sparkle:shortVersionString", namespaces=ns)
        if not sv and encl is not None:
            sv = encl.get(f"{{{ns['sparkle']}}}shortVersionString")
        ver = encl.get(f"{{{ns['sparkle']}}}version") if encl is not None else None
        try:
            vt = tuple(int(x) for x in (sv or ver or "").split("."))
        except (ValueError, TypeError):
            vt = (0,)
        if best_ver is None or vt > best_ver:
            best_ver, best = vt, (sv or ver)
    return (best, None)


# --- GitHub ---
def _extract_github(body: str) -> tuple[str | None, str | None]:
    data = json.loads(body)
    if isinstance(data, list):
        if not data:
            return None, "Empty release list"
        data = data[0]
    if not isinstance(data, dict):
        return None, "Unexpected GitHub response type"
    tag = data.get("tag_name", "")
    ver = tag.lstrip("v").lstrip("release/")
    ver = re.sub(r"^(desktop-|Audacity-|mac-|XQuartz-)", "", ver)
    return (ver, None)


# --- JSON ---
def _extract_json(body: str, extraction: dict) -> tuple[str | None, str | None]:
    data = json.loads(body)
    path = extraction.get("json_path") or extraction.get("path", "version")
    for key in path.lstrip("$").lstrip(".").split("."):
        key = key.split("[")[0]
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list):
            if key.isdigit():
                data = data[int(key)]
            elif data and isinstance(data[0], dict):
                data = data[0].get(key)
            else:
                data = data[0] if data else None
        else:
            return None, f"JSON path '{path}' not found"
    return (str(data) if data is not None else None, None)


# --- YAML ---
def _extract_yaml(body: str, extraction: dict) -> tuple[str | None, str | None]:
    key = extraction.get("key", "version")
    for line in body.splitlines():
        if line.strip().startswith(f"{key}:"):
            ver = line.split(":", 1)[1].strip().strip("'\"")
            return (ver, None)
    return None, f"YAML key '{key}' not found"


# --- HTML scrape ---
def _extract_html(body: str, extraction: dict) -> tuple[str | None, str | None]:
    pattern = extraction.get("pattern", r"(\d+\.\d+(?:\.\d+)?)")
    m = re.search(pattern, body)
    return (m.group(1) if m else None, None)


# ---------------------------------------------------------------------------
# Profile check
# ---------------------------------------------------------------------------
Result = dict  # {slug, name, category, status, version, method, error, url, http_code}


def check_profile(slug: str) -> Result:
    """Check one profile. Returns a result dict."""
    # Find profile file
    path = None
    folder = None
    for d in REPO.iterdir():
        if d.is_dir() and d.name not in (".git", ".github", "scripts", "__pycache__"):
            pf = d / f"{slug}.json"
            if pf.exists():
                path = pf
                folder = d.name
                break
    if not path:
        return {"slug": slug, "name": slug, "status": "BROKEN", "error": "Profile file not found"}

    try:
        profile = json.loads(path.read_text())
    except Exception as e:
        return {"slug": slug, "name": slug, "status": "BROKEN", "error": f"Invalid JSON: {e}"}

    name = profile.get("name", slug)
    vc = profile.get("version_check", {})
    url = (vc.get("url") or "").strip()
    method = normalize_method(vc.get("method", ""))
    extraction = _normalize_extraction(vc.get("extraction"))

    base = {"slug": slug, "name": name, "category": folder, "method": method, "url": url}

    # Skipped methods
    if method in SKIP_METHODS:
        return {**base, "status": "SKIPPED", "error": f"Method '{method}' not testable remotely"}

    # No URL
    if not url:
        return {**base, "status": "UNTESTABLE", "error": "No version URL"}

    # Try to fetch
    code, body = fetch_with_retry(url)
    base["http_code"] = code

    # Transient errors — connection failures, server errors
    if code == -1:
        # Check if it's a bad URL that'll never work
        if body and "Invalid URL" in str(body):
            return {**base, "status": "BROKEN", "error": "Invalid URL in profile"}
        return {**base, "status": "TRANSIENT", "error": f"Connection failed: {str(body)[:80]}"}

    if code in (500, 502, 503, 504):
        return {**base, "status": "TRANSIENT", "error": f"Server error HTTP {code}"}

    if code == 429:
        return {**base, "status": "TRANSIENT", "error": "Rate limited (HTTP 429)"}

    # GitHub rate limiting specifically
    if code == 403 and "api.github.com" in url and not GITHUB_TOKEN:
        return {**base, "status": "TRANSIENT",
                "error": "GitHub rate limited — add GITHUB_TOKEN"}

    # Bot protection / Cloudflare — might work in the app
    if code in (403, 406) and method == "sparkle_appcast":
        return {**base, "status": "DEGRADED", "error": f"HTTP {code} (likely bot protection)"}

    # Genuinely dead URLs
    if code == 404:
        return {**base, "status": "BROKEN", "error": "HTTP 404 — URL not found"}

    if code == 410:
        return {**base, "status": "BROKEN", "error": "HTTP 410 — gone permanently"}

    if code in (400, 401, 402, 405, 407, 408, 409):
        return {**base, "status": "BROKEN", "error": f"HTTP {code}"}

    # Redirects — still works, just moved
    if code in (301, 302, 307, 308):
        # Try to extract anyway if we got a body
        if body:
            return _try_extract(base, method, body, extraction)
        return {**base, "status": "DEGRADED", "error": f"HTTP {code} redirect — no body to parse"}

    if code != 200:
        return {**base, "status": "BROKEN", "error": f"Unexpected HTTP {code}"}

    # Got a 200 response — try to extract version
    return _try_extract(base, method, body, extraction)


def _try_extract(base: Result, method: str, body: str, extraction: dict) -> Result:
    """Try to extract version from a 200 response."""
    try:
        if method in ("sparkle_appcast", "sparkle_rss", "sparkle_xml"):
            version, error = _extract_sparkle(body)
        elif method == "github_api":
            version, error = _extract_github(body)
        elif method in ("json_api", "sourceforge_json"):
            version, error = _extract_json(body, extraction)
        elif method == "yaml_api":
            version, error = _extract_yaml(body, extraction)
        elif method == "scrape_html":
            version, error = _extract_html(body, extraction)
        elif method == "redirect_trace":
            version, error = (f"HTTP 200", None)
        elif method == "plain_text_api":
            pattern = extraction.get("pattern")
            if pattern:
                m = re.search(pattern, body, re.MULTILINE)
                version, error = (m.group(1) if m else None, None)
            else:
                version, error = (body.strip().splitlines()[0], None)
        else:
            return {**base, "status": "UNTESTABLE", "error": f"No extractor for: {method}"}
    except ET.ParseError as e:
        return {**base, "status": "BROKEN", "error": f"XML parse error: {str(e)[:60]}"}
    except json.JSONDecodeError as e:
        return {**base, "status": "BROKEN", "error": f"JSON parse error: {str(e)[:60]}"}
    except Exception as e:
        return {**base, "status": "BROKEN", "error": f"Extraction failed: {str(e)[:80]}"}

    if version:
        return {**base, "status": "HEALTHY", "version": version}
    return {**base, "status": "BROKEN", "error": error or "No version extracted"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Full profile test suite")
    p.add_argument("--slug", default="", help="Test single profile")
    p.add_argument("--sample", type=int, default=0, help="Test random sample")
    p.add_argument("--report", action="store_true", help="Markdown report")
    p.add_argument("--summary", action="store_true", help="Summary only")
    p.add_argument("--badge", action="store_true", help="Shields.io JSON output")
    p.add_argument("--broken-only", action="store_true", help="Only show broken/degraded")
    p.add_argument("--json-out", action="store_true", help="Output JSON results")
    args = p.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text())
    all_slugs = sorted(manifest["apps"].keys())

    if args.slug:
        slugs = [args.slug]
    elif args.sample:
        slugs = random.sample(all_slugs, min(args.sample, len(all_slugs)))
    else:
        slugs = all_slugs

    # Run checks
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(check_profile, s): s for s in slugs}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: (
        {"BROKEN": 0, "DEGRADED": 1, "TRANSIENT": 2, "UNTESTABLE": 3, "SKIPPED": 4, "HEALTHY": 5}[r["status"]],
        r.get("slug", "")
    ))

    # Counts
    counts = defaultdict(int)
    for r in results:
        counts[r["status"]] += 1

    # Badge output
    if args.badge:
        total = counts["HEALTHY"] + counts["BROKEN"] + counts["DEGRADED"]
        pct = (counts["HEALTHY"] + counts["DEGRADED"]) * 100 // total if total else 0
        color = "green" if pct >= 90 else "yellow" if pct >= 70 else "red"
        print(json.dumps({
            "schemaVersion": 1, "label": "profile health",
            "message": f"{pct}%", "color": color,
        }))
        return 0

    # JSON output
    if args.json_out:
        print(json.dumps(results, indent=2))
        return 0

    # Summary
    header = f"Total: {len(results)} | "
    header += " ".join(f"{s}: {c}" for s, c in
                       [("HEALTHY", counts["HEALTHY"]), ("DEGRADED", counts["DEGRADED"]),
                        ("BROKEN", counts["BROKEN"]), ("TRANSIENT", counts["TRANSIENT"]),
                        ("SKIPPED", counts["SKIPPED"]), ("UNTESTABLE", counts["UNTESTABLE"])])

    if args.report:
        print(f"# Profile Test Report\n")
        workable = counts["HEALTHY"] + counts["DEGRADED"]
        total_tested = workable + counts["BROKEN"]
        pct = workable * 100 // total_tested if total_tested else 0
        print(f"**{workable}/{total_tested} usable ({pct}%)** — {header}\n")

        for status, label in [("BROKEN", "Broken — needs fixing"),
                               ("DEGRADED", "Degraded — works with caveats"),
                               ("TRANSIENT", "Transient — retry or check connection"),
                               ("UNTESTABLE", "Untestable — missing URL or unsupported"),
                               ("SKIPPED", "Skipped — not testable remotely")]:
            items = [r for r in results if r["status"] == status]
            if not items:
                continue
            print(f"## {label} ({len(items)})\n")
            for r in items:
                ver = f" → `{r['version']}`" if r.get("version") else ""
                print(f"- **{r['name']}** (`{r['slug']}`) — {r.get('error','?')}{ver} "
                      f"({r.get('method','?')})")
            print()

        # Healthy
        healthy = [r for r in results if r["status"] == "HEALTHY"]
        if healthy and not args.broken_only:
            print(f"## Healthy ({len(healthy)})\n")
            for r in healthy:
                print(f"- {r['name']} (`{r['slug']}`) → `{r.get('version','?')}` ({r.get('method','?')})")
            print()
        return counts["BROKEN"]

    if args.summary:
        print(header)
        return 0

    # Default: full output
    for r in results:
        status = r["status"]
        slug = r["slug"]
        name = r.get("name", slug)
        ver = f" → {r['version']}" if r.get("version") else ""
        err = f" — {r.get('error','?')}" if r.get("error") else ""

        if args.broken_only and status in ("HEALTHY", "SKIPPED"):
            continue

        icon = {"HEALTHY": "✓", "DEGRADED": "~", "BROKEN": "✗",
                "TRANSIENT": "?", "UNTESTABLE": "?", "SKIPPED": "-"}[status]
        print(f"  {icon} {status:11s} {name:30s}{ver:20s} {err}")

    print(f"\n{header}")
    return counts["BROKEN"]


if __name__ == "__main__":
    sys.exit(min(main(), 127))
