# Contributing

## Adding a new app profile

### Step 1: Find the bundle ID

```bash
mdls -name kMDItemCFBundleIdentifier /Applications/Firefox.app
# → "org.mozilla.firefox"
```

Or read Info.plist:
```bash
/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" /Applications/Firefox.app/Contents/Info.plist
```

### Step 2: Find the version check method

Check which update mechanism the app uses:
- **Sparkle**: Look for `SUFeedURL` in `Info.plist`
- **GitHub**: Check if releases are on GitHub (`github.com/owner/repo/releases`)
- **Electron updater**: Try `latest-mac.yml` at the app's CDN
- **Custom API**: Check the app's download/support pages
- **HTML scrape**: Last resort — scrape version from download page

### Step 3: Create the profile

Copy `_template.json` to the correct category folder:

```bash
cp _template.json browsers/new-app.json
```

Fill in all fields. The `version_check.method` must be one of:
- `sparkle_appcast` — Sparkle XML feed
- `github_api` — GitHub Releases API
- `json_api` — JSON REST endpoint
- `yaml_api` — YAML endpoint (Electron updater)
- `scrape_html` — HTML page scraping
- `itunes_api` — Mac App Store (iTunes Lookup API)
- `redirect_trace` — Follow HTTP redirect
- `plain_text_api` — Plain text version endpoint
- `download_and_parse_plist` — Download + extract Info.plist
- `sourceforge_json` — SourceForge best_release API

### Step 4: Update the manifest

```bash
python3 scripts/generate_manifest.py
```

### Step 5: Validate

```bash
python3 scripts/validate_profiles.py
```

### Step 6: Open a Pull Request

Push your branch and open a PR. CI will validate your profile automatically.

## Choosing the right category

| Category | For |
|----------|-----|
| `browsers` | Web browsers |
| `developer-tools` | IDEs, editors, terminals, git clients, DB tools |
| `design` | Design tools, photo editors, graphic apps |
| `media` | Video/audio players, streaming, DAWs |
| `messaging` | Chat, video calls, team comms |
| `notes-writing` | Notes, writing, knowledge management |
| `utilities` | System tools, launchers, clipboard, window managers |
| `vpn-security` | VPNs, firewalls, security tools |
| `cloud-backup` | File sync, backup, cloud storage |
| `office` | Office suites, spreadsheets, e-books |
| `email` | Email clients |
| `gaming` | Game launchers, gaming platforms |
| `virtualization` | VMs, emulation |
| `remote-desktop` | Remote desktop, VNC |
| `password-managers` | Password management |
| `ai-tools` | AI assistants, copilots |
| `rss-reading` | RSS readers, PDF readers |
| `specialized` | Niche tools that don't fit elsewhere |
| `other` | Everything else |

## Profile quality checklist

- [ ] `slug` is lowercase, URL-safe, unique
- [ ] `bundle_id` is correct (verified with mdls/PlistBuddy)
- [ ] `version_check.url` is a real, working URL
- [ ] `version_check.extraction` has correct XPath/JSON path/regex
- [ ] `download.url` or `download.pattern` produces a working download link
- [ ] `license` is correct (free = works without payment, paid = requires purchase)
- [ ] `skip: true` only for discontinued apps
- [ ] `last_updated` is today's date
