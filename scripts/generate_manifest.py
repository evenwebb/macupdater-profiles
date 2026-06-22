#!/usr/bin/env python3
"""Regenerate manifest.json from all profile JSON files."""
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".github", "scripts", "__pycache__"}


def main():
    apps = {}
    count = 0
    for d in sorted(REPO.iterdir()):
        if not d.is_dir() or d.name in SKIP_DIRS or d.name.startswith("."):
            continue
        for f in sorted(d.glob("*.json")):
            if f.name.startswith("_"):
                continue
            try:
                data = json.loads(f.read_text())
            except json.JSONDecodeError as e:
                print(f"  SKIP {f.relative_to(REPO)}: invalid JSON: {e}")
                continue
            slug = data.get("slug", f.stem)
            if not slug:
                print(f"  SKIP {f.relative_to(REPO)}: no slug")
                continue
            apps[slug] = {
                "name": data.get("name", slug),
                "bundle_id": data.get("bundle_id"),
                "alternate_bundle_ids": data.get("alternate_bundle_ids", []),
                "icons": data.get("icons", {}),
                "category": d.name,
                "license": data.get("license", "free"),
                "path": str(f.relative_to(REPO)),
                "skip": data.get("skip", False),
            }
            count += 1

    manifest = {
        "schema_version": 3,
        "version": 3,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_profiles": count,
        "apps": dict(sorted(apps.items())),
    }

    # Preserve existing timestamp if apps haven't actually changed
    manifest_path = REPO / "manifest.json"
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text())
            if old.get("apps") == manifest["apps"] and old.get("total_profiles") == count:
                manifest["updated"] = old.get("updated", manifest["updated"])
        except (json.JSONDecodeError, KeyError):
            pass

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Manifest updated: {count} profiles")


if __name__ == "__main__":
    main()
