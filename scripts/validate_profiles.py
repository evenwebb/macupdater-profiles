#!/usr/bin/env python3
"""Validate all profiles against schema.json. Exit 1 on any failure."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".github", "scripts", "__pycache__"}

REQUIRED = ["name", "slug", "category", "license", "version_check", "download"]
VALID_LICENSES = {"free", "paid"}
# Version check methods
VALID_VERSION_METHODS = {
    "sparkle_appcast", "github_api", "json_api", "yaml_api",
    "scrape_html", "itunes_api", "redirect_trace",
    "download_and_parse_plist", "plain_text_api", "sourceforge_json",
    "git_self_update", "microsoft_autoupdate", "none", "None", "url_checksum_only",
}

# Download methods
VALID_DOWNLOAD_METHODS = {
    "direct_url", "redirect_url", "construct_url", "pattern",
    "app_store_only", "source_build_only", "sparkle_enclosure",
    "form_post_with_csrf", "extracted_from_json", "extracted_from_version_check",
    "yaml_field", "json_field", "web_redirect", "composed_url",
    "constructed_url", "versioned_url", "fixed_url", "direct",
    "scrape", "git_pull", "sparkle_enclosure", "mac_app_store",
    "macappstore", "dynamic_cdn_url", "construct_per_macos",
    "construct_url_or_suite", "construct_from_github_tag",
    "scrape_website", "extract_from_sparkle", "custom_api",
    "broadcom_portal_only", "url_checksum_only", "construct", "dynamic_portal_only",
}

errors = 0
count = 0

for d in sorted(REPO.iterdir()):
    if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
        continue
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue
        count += 1
        rel = f.relative_to(REPO)
        try:
            data = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            print(f"FAIL {rel}: invalid JSON: {e}")
            errors += 1
            continue

        for field in REQUIRED:
            if not data.get(field):
                print(f"FAIL {rel}: missing required field '{field}'")
                errors += 1

        lic = data.get("license", "")
        if lic and lic not in VALID_LICENSES:
            print(f"FAIL {rel}: invalid license '{lic}'")
            errors += 1

        vc = data.get("version_check", {})
        method = vc.get("method", "")
        if method and method not in VALID_VERSION_METHODS:
            print(f"FAIL {rel}: unknown version_check method '{method}'")
            errors += 1

        dl = data.get("download", {})
        dl_method = dl.get("method", "")
        if dl_method and dl_method not in VALID_DOWNLOAD_METHODS:
            print(f"FAIL {rel}: unknown download method '{dl_method}'")
            errors += 1

        
        slug = data.get("slug", "")
        if not slug:
            print(f"FAIL {rel}: missing required field 'slug'")
            errors += 1

# Check manifest
manifest_path = REPO / "manifest.json"
if manifest_path.exists():
    try:
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("total_profiles", 0) != count:
            print(f"FAIL manifest.json: total_profiles={manifest['total_profiles']} but found {count}")
            errors += 1
    except json.JSONDecodeError as e:
        print(f"FAIL manifest.json: invalid JSON: {e}")
        errors += 1
else:
    print("FAIL: manifest.json missing")
    errors += 1

if errors:
    print(f"\n{errors} error(s) in {count} profile(s)")
    sys.exit(1)

print(f"OK: {count} profile(s) validated")
