# Google Photos Uploader

A command-line tool for bulk uploading photos from an external hard drive to Google Photos. Built for reliability on large archives (5,000–20,000+ files) with resumable state, local compression, and a polished terminal experience.

## Quick Start

```bash
git clone https://github.com/condedanilo/google-photos-uploader.git
cd google-photos-uploader
uv sync

gphotos init                    # guided setup: config + Google Cloud + auth
gphotos upload ~/Pictures       # start uploading
```

`gphotos init` walks you through everything, including obtaining the Google Cloud credential. Run it once and you're done.

## Features

- **Resumable** — interrupted runs pick up exactly where they left off
- **Deduplication** — files are identified by content hash (SHA-256), not filename; moved or renamed files are never uploaded twice
- **Local compression** — compress images before upload to save Google Photos storage; choose from three preset levels
- **Parallel uploads** — configurable worker threads for faster byte uploads; `batchCreate` calls are serialized per API requirements
- **Real-time progress** — live display with files/min, ETA, and running compression savings
- **Graceful shutdown** — Ctrl+C saves state cleanly; double Ctrl+C for immediate exit
- **Human-readable errors** — API and OS errors translated to plain language with actionable guidance
- **Cross-platform** — runs on macOS, Linux, and Windows

## Installation

### Requirements
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Install with uv

```bash
git clone https://github.com/condedanilo/google-photos-uploader.git
cd google-photos-uploader
uv sync
```

After `uv sync`, the `gphotos` command is available in two ways:

**Option A — prefix with `uv run` (no activation needed):**
```bash
uv run gphotos auth login
uv run gphotos upload /path/to/photos
```

**Option B — activate the virtual environment:**
```bash
source .venv/bin/activate   # macOS / Linux
# .venv\Scripts\activate    # Windows

gphotos auth login           # works directly while venv is active
deactivate                   # when done
```

### Linux — HEIC support

HEIC files require `libheif` on Linux:

```bash
# Debian / Ubuntu
sudo apt install libheif1

# Fedora
sudo dnf install libheif
```

macOS and Windows: HEIC support is bundled automatically via `pillow-heif`.

## Google Cloud Setup

You need a one-time OAuth credential from Google. `gphotos init` reminds you of these steps if you haven't done them yet.

1. Go to **[console.cloud.google.com](https://console.cloud.google.com)** and sign in
2. Create a new project (top-left dropdown → **New Project**)
3. Enable the **Google Photos Library API**
   - **APIs & Services → Library** → search *Photos Library API* → **Enable**
4. Create an OAuth client ID
   - **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app** — give it any name
5. Click **Download JSON** and save the file as:
   ```
   ~/.config/gphotos-uploader/client_secret.json
   ```
6. Run `gphotos init` — it will open your browser for a one-time login

> **Tip:** If you see a "Google hasn't verified this app" warning, click **Advanced → Go to (app name) (unsafe)**. This is expected for personal OAuth apps.

## Configuration

`~/.config/gphotos-uploader/uploader.toml` is created automatically with sensible defaults on the first run. Open it in any text editor to customise the tool's behaviour.

Key settings:

```toml
[paths]
credentials = "~/.config/gphotos-uploader/client_secret.json"
token       = "~/.config/gphotos-uploader/token.json"
state_db    = "~/.local/share/gphotos-uploader/state.db"

[upload]
workers    = 4      # parallel upload threads
max_retries = 3

[compression]
enabled = true
level   = "mid"     # "low" | "mid" | "high"
```

CLI flags override config file values.

## Usage

### First run — guided setup

```bash
gphotos init
```

Walks through config creation, credential verification, and the one-time Google OAuth browser flow. The token is saved locally and refreshed automatically on subsequent runs.

You can also trigger the OAuth flow directly at any time:

```bash
gphotos auth login
```

### Upload photos

```bash
gphotos upload /Volumes/MyDrive/Photos
```

The tool will:
1. Scan the directory recursively and show a live file count
2. Compute content hashes and identify already-uploaded files
3. Show a pre-upload summary and ask for confirmation
4. Upload files in parallel with live progress
5. Show a final report with compression savings and any errors

### Resume an interrupted run

Just run the same command again — the tool detects the saved state and asks whether to continue or start over.

### Common flags

```bash
# Use aggressive compression to maximize storage savings
gphotos upload /path/to/photos --compression-level high

# Skip compression entirely (upload original files)
gphotos upload /path/to/photos --no-compress

# Use 6 parallel workers instead of the default 4
gphotos upload /path/to/photos --workers 6

# Skip all confirmation prompts (useful for scripting)
gphotos upload /path/to/photos --yes

# Discard saved state and start from scratch
gphotos upload /path/to/photos --reset
```

### Check auth status

```bash
gphotos auth status
```

## Compression Levels

| Level  | JPEG Quality | Typical Savings | Notes |
|--------|-------------|-----------------|-------|
| `low`  | 92           | ~30–40%         | Minimal quality loss, barely distinguishable |
| `mid`  | 85           | ~50–60%         | Google "Storage Saver" equivalent **(default)** |
| `high` | 60           | ~70–80%         | Aggressive; slight quality loss visible on close inspection |

Videos (MP4, MOV, etc.) are uploaded as-is regardless of compression settings.

The final report shows the total original size, uploaded size, and bytes saved.

## Progress Display

During upload:
```
Uploading ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 68%  3,412/5,000  ✗ 2  Saved 9.4 GB (57%)  ETA 1h12m
```

Final report:
```
╭──────────────────────────────────────────────────────────╮
│  Upload Complete                                         │
├──────────────────────────────────────────────────────────┤
│  Total      Uploaded   Skipped   Errors   Duration       │
│  5,000      4,996      2         2        2h 34m          │
├──────────────────────────────────────────────────────────┤
│  Compression Summary (level: mid, q=85)                  │
│  Original:   42.3 GB                                     │
│  Uploaded:   18.1 GB                                     │
│  Saved:      24.2 GB  (57%)                              │
╰──────────────────────────────────────────────────────────╯
```

## Troubleshooting

| Error | Solution |
|-------|----------|
| `Could not refresh your Google token` | Run `gphotos auth login` to re-authenticate |
| `Daily quota exhausted` | Re-run tomorrow — state is saved, progress is preserved |
| `File not found` | The file was moved or deleted after the scan started; it will be skipped |
| `HEIC files not compressing` on Linux | Install `libheif1`: `sudo apt install libheif1` |

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
uv run pytest

# Run a single test file
uv run pytest tests/unit/test_hasher.py -v
```

## Security

- OAuth credentials (`client_secret.json`) and tokens (`token.json`) are **never** logged or committed — they are listed in `.gitignore`
- The OAuth scope is the minimum required: `photoslibrary.appendonly` (write-only; cannot read or delete your library)
- The state database contains only file paths and hashes, no credentials

## Roadmap

See [PRD_Photo_Uploader_EN.md](PRD_Photo_Uploader_EN.md) for the full roadmap.

**v2 (planned):** `--dry-run` mode, `--status` command, ASCII dashboard, concurrent execution lock
**v3 (planned):** Duplicate detection in Google Photos, album mapping, LLM-powered tagging
