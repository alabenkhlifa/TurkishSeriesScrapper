# TurkishSeriesScrapper

## Project Overview
Automated Turkish series downloader and Plex media manager. Scrapes episodes from **krmzi.org**, downloads them, integrates with Plex Server, and auto-cleans watched episodes. Runs as a cron job on a Raspberry Pi 5.

## Target Environment
- **Hardware**: Raspberry Pi 5
- **Storage**: SSD mounted at `/dev/sda2`
- **Media Server**: Plex v1.42.1 (connected to TV)
- **Assistant**: OpenClaw running 24/7 with Claude
- **OS**: Raspberry Pi OS (ARM64)
- **Cron Schedule**: Every 4 hours

## Website Structure (krmzi.org)
- **Series list**: `https://krmzi.org/series-list/` (paginated, 5 pages, ~150+ series)
- **Individual series**: `https://krmzi.org/series/{arabic-slug}/`
- **Episodes**: `https://krmzi.org/episode/{series-slug}-الحلقة-{number}/`
- **Episodes listed** in reverse chronological order on series pages (newest first)
- **Built on**: WordPress with `dark_vo` theme, Cloudflare Turnstile protection
- **Content**: Turkish series with Arabic subtitles/dubbing

## Download Pipeline (step by step)
1. **Scrape episode page** (`krmzi.org/episode/{slug}-الحلقة-{N}/`)
   → Extract the play button redirect URL which points to `qesen.net/krmzi/?post={base64}`
2. **Decode the base64 `post` parameter** → JSON payload with structure:
   ```json
   {
     "codeDaily": "",
     "servers": [
       {"name": "Arab HD", "id": "gb6nn2dsv3pl"},
       {"name": "estream", "id": "oajl3b4g8nf1"},
       {"name": "express", "id": "https://cloud.mail.ru/public/XXXX/XXXXXXX"},
       {"name": "ok", "id": "12084751370753"},
       {"name": "Pro HD", "id": "6447mt3rjvqt"},
       {"name": "Red HD", "id": "ro0e27jlhcaf"}
     ],
     "postID": "7613",
     "type": "episodes",
     "backUrl": "https://krmzi.org/episode/..."
   }
   ```
3. **Pick the "express" server** → its `id` is a direct `cloud.mail.ru/public/` link
4. **Download via mail.ru API** (no yt-dlp needed):
   - GET the public page → establish cookies, extract `weblink_get` base URL from embedded JSON
   - GET `/api/v2/tokens/download` → get a download token
   - Download from `{weblink_get_url}/{public_hash}?key={token}`
   - This gives the full-quality original uploaded file directly

## Core Features
1. **Series Selection** - User picks which series to track from krmzi.org catalog
2. **Episode Scraping** - Cron job (every 4h) checks tracked series for new episodes
3. **Episode Downloading** - Downloads new episodes and places them in Plex-compatible folder structure
4. **Plex Integration** - Notifies Plex of new media, queries watch status via Plex API
5. **Auto-Cleanup** - Deletes episodes marked as watched in Plex to free SSD space

## Tech Stack
- **Language**: Python 3
- **Scraping**: requests + BeautifulSoup4
- **Downloading**: Direct HTTP download from cloud.mail.ru via their API (no yt-dlp)
- **Plex API**: plexapi (python-plexapi library)
- **Scheduling**: systemd timer or cron
- **Config**: YAML config file for tracked series and preferences
- **No database needed** - filesystem + Plex API are the source of truth

## Directory Structure
```
TurkishSeriesScrapper/
├── CLAUDE.md
├── .gitignore
├── requirements.txt
├── config.yaml                # User config (gitignored, contains secrets)
├── config.yaml.example        # Template config (committed)
├── scrapper.py                # Single-file script: scraping, downloading, Plex integration
└── logs/                      # Rotating log files (gitignored)
```

## Plex Media Folder Layout
Episodes must follow Plex naming conventions:
```
/mnt/nextcloud/plex-server/media/TurkishSeries/
└── {Series Name}/
    └── Season 01/
        └── {Series Name} - S01E{XX}.mp4
```

## Key Implementation Notes
- Website uses Cloudflare Turnstile - browser-like headers are used to handle this
- Always use the "express" server from the decoded payload - it has a direct cloud.mail.ru link
- mail.ru download uses their API (dispatcher + tokens/download) - no video player parsing needed
- Arabic URL slugs are URL-encoded - handle encoding/decoding properly
- Series don't have explicit type labels on the site - the user maintains their own watchlist in config.yaml
- Plex API token is required - store in config.yaml or environment variable
- Always check SSD free space before downloading
- Log all operations for debugging on headless Pi

## Config Format (config.yaml)
```yaml
plex:
  url: "http://localhost:32400"
  token: "YOUR_PLEX_TOKEN"
  library_name: "Turkish Series"

storage:
  media_root: "/mnt/nextcloud/plex-server/media/TurkishSeries"
  min_free_space_gb: 10

scraper:
  base_url: "https://krmzi.org"
  check_interval_hours: 4

series:
  - name: "ورود و ذنوب"
    slug: "مسلسل-ورود-و-ذنوب"
    enabled: true
  - name: "تحت الأرض"
    slug: "مسلسل-تحت-الأرض"
    enabled: true
  - name: "حلم أشرف"
    slug: "مسلسل-حلم-أشرف"
    enabled: true
  - name: "هذا البحر سوف يفيض"
    slug: "مسلسل-هذا-البحر-سوف-يفيض"
    enabled: true
  - name: "المدينة البعيدة"
    slug: "مسلسل-المدينة-البعيدة"
    enabled: true

download:
  server: "express"          # always use express (cloud.mail.ru, downloads original file)
```

## Development Guidelines
- Test scraping logic against live site carefully - respect rate limits
- Use proper User-Agent headers
- Handle network failures gracefully (Pi may have intermittent connectivity)
- All file paths should use pathlib for cross-platform safety
- Keep logs rotated to avoid filling storage
- New episode detection: check if file exists on disk, if not → download
- Cleanup: ask Plex what's watched → delete those files
