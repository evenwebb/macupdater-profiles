# MacUpdater Profiles

Community-maintained update profiles for macOS applications. Used by [MacUpdater](https://github.com/evenwebb/MacUpdater) to check for and install app updates.

## Quick Start

```bash
git clone https://github.com/evenwebb/macupdater-profiles.git
```

Each `.json` file is an app profile. Install MacUpdater to use them automatically.

## Profile Structure

Profiles are organized by category. Each profile is a JSON file:

```
browsers/firefox.json
developer-tools/vscode.json
utilities/bartender.json
...
```

## Adding a Profile

1. Copy `_template.json` and fill in the fields
2. Place it in the correct category folder
3. Run `python3 scripts/generate_manifest.py` to update manifest.json
4. Open a PR

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed instructions.

## Profile Schema

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Human-readable app name |
| `slug` | Yes | URL-safe unique ID (`a-z0-9-`) |
| `bundle_id` | Recommended | macOS bundle identifier (`com.example.app`) |
| `category` | Yes | Category folder name |
| `license` | Yes | `"free"` or `"paid"` |
| `description` | No | Short description |
| `website` | No | App homepage |
| `skip` | No | `true` if app is discontinued/unmaintained |
| `auto_updates` | No | `true` if app self-updates |
| `version_check` | Yes | How to check latest version |
| `download` | Yes | Where to download |
| `changelog` | No | Where to find release notes |
| `release_date` | No | How to get release date |
| `last_updated` | No | ISO date when profile was last changed |

## License

CC0 — public domain. Anyone can contribute, anyone can use.
