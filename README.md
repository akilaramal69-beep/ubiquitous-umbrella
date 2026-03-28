# Telegram URL Uploader Bot

A powerful Telegram bot that uploads files up to **2 GB** directly to Telegram from any URL — including Instagram, TikTok, Twitter/X, and 700+ more platforms. Built with Pyrogram (MTProto) for large file support.

**This version includes integrated link-api for direct media extraction without external dependencies.**

---

## Features

| Feature | Details |
|---------|---------|
| 📤 Direct URL Upload | Send any direct download URL — bot downloads & uploads |
| 📺 yt-dlp Integration | Download from Instagram, TikTok, Twitter/X, Reddit, Facebook, Vimeo + 700 more |
| ✏️ File Renaming | Bot asks for a new filename before every upload |
| 🎬 Media / Document mode | Choose to send as streamable video or raw document |
| 🎵 Audio Extraction | Extract high-quality audio tracks directly from video links |
| 🎞️ Auto Thumbnail | ffmpeg auto-generates thumbnail from video frame |
| ⏱️ Video Metadata | ffprobe extracts duration, width, height for proper Telegram video display |
| 🌊 HLS / DASH streams | `.m3u8`, `.mpd`, `.ts` streamed via ffmpeg → saved as `.mp4` |
| 💾 Up to 2 GB | Pyrogram MTProto — not the 50 MB Bot API limit |
| 📝 Custom Captions | Per-user saved captions |
| 🖼️ Permanent Thumbnails | Stored as Telegram `file_id` — survive restarts & redeployments |
| ✨ Custom Watermarks | Premium-only: Text or Image overlays on thumbnails, adjustable color/size/opacity |
| 🎞️ AI Subtitles     | Premium-only: Auto-generate `.srt` or burn into video after transcription |
| 🖼️ One-time Thumbnails| Premium-only: Set a custom thumbnail for a single upload via interactive button |
| 📊 Live Progress | Real-time progress bars in chat |
| 🚀 Upload Boost | pyroblack `upload_boost=True` + parallel MTProto connections |
| ⭐ Premium System | Free users: 50 downloads/day, Premium: unlimited |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Telegram Bot (Pyrogram)                │
│                   MTProto API (2GB limit)                  │
└──────────────────────────┬──────────────────────────────────┘
                           │
         ┌─────────────────┴─────────────────┐
         │                                   │
         ▼                                   ▼
┌───────────────┐                 ┌──────────────┐
│ Flask Server  │                 │  yt-dlp      │
│ (Health API)  │                 │  Extractor   │
│ Port 8080     │                 │              │
└───────────────┘                 └──────────────┘
```

---

## Quick Start (Docker)

### 1. Clone and Deploy

```bash
git clone https://github.com/yourusername/telelinkworking.git
cd telelinkworking
```

### 2. Configure Environment Variables

Create a `.env` file:

```env
# Required
BOT_TOKEN=your_bot_token_from_botfather
API_ID=your_api_id_from_my_telegram_org
API_HASH=your_api_hash_from_my_telegram_org
OWNER_ID=your_telegram_user_id
DATABASE_URL=mongodb_connection_string
LOG_CHANNEL=-1001234567890

# Optional - Leave empty for local extraction
LINK_API_URL=
COBALT_API_URL=
```

### 3. Deploy to Koyeb (Docker)

1. Push to GitHub
2. Create service on Koyeb → Select **Docker**
3. Add environment variables
4. Set Port to **8080**
5. Deploy!

---

## Local Development

### Prerequisites

- Python 3.11+
- FFmpeg
- MongoDB (local or Atlas)
- Chromium (auto-installed via Playwright)

### Installation

```bash
# Clone repository
git clone https://github.com/yourusername/telelinkworking.git
cd telelinkworking

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Run the bot
python bot.py
```

---

## Bot Commands

```
/start           – Start the bot 🔔
/help            – Show all commands ❓
/about           – Bot info ℹ️
/ upload <url>   – Upload file from URL 📤
/skip            – Keep original filename during rename
/status          – View your daily download stats 📊

/caption <text>  – Set custom upload caption 📝
/showcaption     – View your caption
/clearcaption    – Clear caption

/setthumb        – Reply to a photo to set permanent thumbnail 🖼️
/showthumb       – Preview your thumbnail
/delthumb        – Delete thumbnail

--- Premium Features ⭐ ---
/setwatermark    – <text> [pos] or reply to a photo to set watermark
/wmcolor <hex>   – Set text color (e.g. #ffffff)
/wmopacity <num> – Set opacity from 0 to 100
/wmsize <num>    – Set size percentage from 1 to 100
/showwatermark   – View your watermark settings
/clearwatermark  – Remove watermark
/setsubs <on/off>– Toggle AI subtitle generation 📝
/sublang <lang>  – Set subtitle language (en, ja, auto, etc)
/submethod <local/api> – Switch AI method (Local or API)
/submodel <base/small/distil-large-v3/medium/large-v3> – Set local AI model

### 4. Optimize for 4GB RAM
If you have a 4GB RAM instance (like on Koyeb), the **distil-large-v3** model is the absolute best choice for professional accuracy and speed!
```
/submodel distil-large-v3
```
After download, the bot will ask you if you want to receive the `.srt` or burn it!
/substats        – View current subtitle settings

--- Admin only ---
/broadcast <msg> – Broadcast to all users 📢
/total           – Total registered users 👥
/ban <id>        – Ban a user ⛔
/unban <id>      – Unban a user ✅
/premium <id>    – Toggle premium status ⭐
/statusall       – CPU / RAM / Disk stats 🚀
```

---

## Built-in Link API

This bot includes an integrated link extraction API powered by Playwright. No external service required!

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/grab?url=<URL>` | Get best direct download link |
| POST | `/grab` | Same with JSON body |
| POST | `/extract` | Get full yt-dlp compatible JSON |

### Example Usage

```bash
# Get best link
curl "http://localhost:8080/grab?url=https://example.com/video"

# Get full extraction
curl -X POST "http://localhost:8080/extract" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/video"}'
```

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | From @BotFather |
| `API_ID` | From my.telegram.org |
| `API_HASH` | From my.telegram.org |
| `OWNER_ID` | Your Telegram user ID |
| `DATABASE_URL` | MongoDB connection string |
| `LOG_CHANNEL` | Private channel ID for upload logs |
| `PREMIUM_USERS`| Space-separated list of user IDs to grant instant premium |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_USERNAME` | UrlUploaderBot | Bot username |
| `ADMIN` | - | Space-separated admin user IDs |
| `BANNED_USERS` | - | Space-separated banned user IDs |
| `LINK_API_URL` | (empty) | External link API (uses built-in if empty) |
| `COBALT_API_URL` | (empty) | Cobalt API for Instagram/TikTok fallback |
| `SESSION_STRING` | - | Premium session for 4GB uploads |
| `CHUNK_SIZE` | 10240 | Upload chunk size in KB |
| `COOKIES_FILE` | cookies.txt | Path to cookies file |
| `PROXY` | - | Proxy URL |
| `FFMPEG_PATH` | ffmpeg | Path to FFmpeg |
| `GROQ_API_KEY` | - | For high-accuracy API-based subtitles |
| `OPENAI_API_KEY`| - | Alternative API for subtitles |

---

## Premium System

- **Free users:** 50 downloads per day
- **Premium users:** Unlimited downloads + Watermarks + **AI Subtitle Generation**
- **Admins & Owner:** Unlimited downloads (always)

To manage premium users:
```
/premium <user_id>      - Check premium status
/premium <user_id> on   - Enable premium
/premium <user_id> off  - Disable premium
```

---

## Project Structure

```
telelinkworking/
├── bot.py                  # Main entry point
├── app.py                  # Flask server + Link API endpoints
├── requirements.txt        # Python dependencies
├── Dockerfile              # Docker configuration
├── plugins/
│   ├── config.py           # Environment configuration
│   ├── commands.py         # Bot command handlers
│   ├── admin.py            # Admin commands
│   └── helper/
│       ├── upload.py       # Download & upload logic
│       ├── extractor.py    # Link extraction orchestration
│       ├── browser_extractor.py  # Playwright-based extraction
│       └── database.py     # MongoDB operations
└── utils/
    ├── shared.py          # Shared state
    └── subtitles.py       # AI Subtitle Generation logic
```

---

## Troubleshooting

### Bot won't start?

- Check that `BOT_TOKEN`, `API_ID`, and `API_HASH` are set
- Verify MongoDB connection string is valid

### Playwright errors?

- Run `playwright install chromium` manually
- Check that required system libraries are installed

### Instagram/TikTok failing?

- The bot will automatically fall back to Cobalt API if configured
- Set `COBALT_API_URL` for Instagram/Pinterest fallback
- Or use cookies for authenticated downloads

### Memory issues?

- The bot cleans the DOWNLOADS folder on startup

---

## Credits

- [Pyrogram](https://pyrogram.org/) - Telegram MTProto client
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - Video downloader
- [Playwright](https://playwright.dev/) - Browser automation
- [Koyeb](https://koyeb.com/) - Deployment platform
