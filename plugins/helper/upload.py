import asyncio
import time
import os
import json
import mimetypes
import re
import shutil
import urllib.parse
from PIL import Image, ImageDraw, ImageFont
import aiohttp
import aiofiles
from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import aria2p
from plugins.config import Config
from utils.shared import get_http_session

PROGRESS_UPDATE_DELAY = 1  # seconds between progress edits


# ── Watermark helper ──────────────────────────────────────────────────────────

VALID_POSITIONS = {
    "top-left", "top-center", "top-right",
    "center-left", "center", "center-right",
    "bottom-left", "bottom-center", "bottom-right",
}


def apply_watermark(img: Image.Image, watermark: dict, wm_image_path: str = None) -> Image.Image:
    """
    Overlay a text or image watermark on a PIL thumbnail image.
    Supports customizations for opacity, color, size, and position.
    """
    img = img.convert("RGBA")
    W, H = img.size

    position = watermark.get("position", "bottom-right") or "bottom-right"
    opacity = watermark.get("opacity", 90) / 100.0  # 0.0 to 1.0
    size_pct = watermark.get("size", 10) / 100.0    # 0.0 to 1.0

    margin = int(min(W, H) * 0.05)

    if wm_image_path:
        with Image.open(wm_image_path) as wm_img:
            wm_img = wm_img.convert("RGBA")
            target_w = max(10, int(W * size_pct))
            
            # Use LANCZOS for resizing
            wm_img.thumbnail((target_w, target_w), Image.Resampling.LANCZOS)
            box_w, box_h = wm_img.size
            
            # Apply opacity
            if opacity < 1.0:
                alpha = wm_img.split()[3]
                alpha = alpha.point(lambda p: int(p * opacity))
                wm_img.putalpha(alpha)
                
            bx, by = calculate_wm_position(position, W, H, box_w, box_h, margin)
            img.alpha_composite(wm_img, (bx, by))
            return img.convert("RGB")

    # Text watermark
    text = watermark.get("text")
    if not text:
        return img.convert("RGB")
        
    color_str = watermark.get("color", "#ffffff")
    try:
        from PIL import ImageColor
        color_rgb = ImageColor.getrgb(color_str)
    except:
        color_rgb = (255, 255, 255)

    # Convert opacity to alpha (0-255)
    alpha_val = int(255 * opacity)
    color_rgba = (*color_rgb, alpha_val)

    # Font sizing
    font_size = max(12, int((H * size_pct)))
    font = None
    for candidate in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        if os.path.isfile(candidate):
            try:
                font = ImageFont.truetype(candidate, font_size)
                break
            except Exception:
                pass
    if font is None:
        try:
            font = ImageFont.load_default(size=font_size)
        except TypeError:
            font = ImageFont.load_default()

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    # Percentage-based padding and margin for total consistency
    # Use the smaller dimension to keep the gap visually consistent across aspect ratios
    base_dim = min(W, H)
    margin = int(base_dim * 0.05)
    padding = int(base_dim * 0.02)
    
    box_w = tw + padding * 2
    box_h = th + padding * 2

    bx, by = calculate_wm_position(position, W, H, box_w, box_h, margin)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Background rectangle with rounded look
    draw.rectangle(
        [(bx, by), (bx + box_w, by + box_h)],
        fill=(0, 0, 0, int(155 * opacity))
    )
    
    draw.text(
        (bx + padding, by + padding),
        text,
        font=font,
        fill=color_rgba
    )

    composite = Image.alpha_composite(img, overlay)
    return composite.convert("RGB")


def calculate_wm_position(position: str, W: int, H: int, box_w: int, box_h: int, margin: int = 15):
    position = position.lower() if position else "bottom-right"
    if position not in VALID_POSITIONS:
        position = "bottom-right"

    v, h = (position.split("-") + ["center"])[:2] if "-" in position else (position, "center")

    if v == "top":
        by = margin
    elif v == "bottom":
        by = H - box_h - margin
    else:  # center
        by = (H - box_h) // 2

    if h == "left":
        bx = margin
    elif h == "right":
        bx = W - box_w - margin
    else:  # center
        bx = (W - box_w) // 2
        
    return bx, by




def _get_ffmpeg_bin() -> str:
    """Return the actual ffmpeg binary path, checking FFMPEG_PATH and PATH."""
    path = Config.FFMPEG_PATH  # could be 'ffmpeg' or '/usr/bin/ffmpeg'
    # If it already looks like a binary (has no dir separators), use shutil.which
    if os.sep not in path and '/' not in path:
        found = shutil.which(path)
        if found:
            return found
    if os.path.isfile(path):
        return path
    # Last resort: try well-known locations
    for candidate in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if os.path.isfile(candidate):
            return candidate
    return path  # return whatever was configured, let the caller fail


def _get_ffmpeg_dir() -> str:
    """Return the DIRECTORY containing ffmpeg — what yt-dlp expects for ffmpeg_location."""
    return os.path.dirname(_get_ffmpeg_bin()) or None


def _get_ffprobe_bin() -> str:
    """Return the ffprobe binary path (same dir as ffmpeg)."""
    ffmpeg_dir = os.path.dirname(_get_ffmpeg_bin())
    ffprobe = os.path.join(ffmpeg_dir, "ffprobe") if ffmpeg_dir else "ffprobe"
    if os.path.isfile(ffprobe):
        return ffprobe
    found = shutil.which("ffprobe")
    return found or "ffprobe"

# ── Streaming / HLS detection ─────────────────────────────────────────────────

# Extensions that indicate a playlist / stream, not a direct media file
STREAMING_EXTENSIONS: dict[str, str] = {
    ".m3u8": ".mp4",
    ".m3u":  ".mp4",
    ".mpd":  ".mp4",   # DASH manifest
    ".ts":   ".mp4",   # raw MPEG-TS segment
}

HLS_MIME_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "video/mp2t",
}

# Media type detection
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico', '.tiff', '.tif'}
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.3gp', '.ts', '.m2ts'}
AUDIO_EXTS = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma', '.opus'}
ARCHIVE_EXTS = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz'}
DOCUMENT_EXTS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.csv', '.epub', '.mobi'}

MEDIA_TYPES = ('video/', 'audio/', 'image/')
VIDEO_MIMES = {'video/mp4', 'video/webm', 'video/x-matroska', 'video/quicktime', 'video/x-msvideo'}
AUDIO_MIMES = {'audio/mpeg', 'audio/mp4', 'audio/webm', 'audio/wav', 'audio/flac', 'audio/ogg'}
IMAGE_MIMES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp', 'image/svg+xml'}


def is_media_url(url: str, mime: str = None) -> tuple[bool, str]:
    """
    Check if URL is a media file (video/audio/image).
    Returns (is_media, media_type) where media_type is 'video', 'audio', 'image', or 'unknown'
    """
    path = urllib.parse.urlparse(url).path.lower()
    ext = os.path.splitext(path)[1]
    
    # Check by extension
    if ext in IMAGE_EXTS:
        return True, 'image'
    if ext in VIDEO_EXTS:
        return True, 'video'
    if ext in AUDIO_EXTS:
        return True, 'audio'
    
    # Check by MIME type if provided
    if mime:
        mime_lower = mime.lower()
        if any(mime_lower.startswith(m) for m in MEDIA_TYPES):
            if mime_lower.startswith('video/'):
                return True, 'video'
            if mime_lower.startswith('audio/'):
                return True, 'audio'
            if mime_lower.startswith('image/'):
                return True, 'image'
        if mime_lower in VIDEO_MIMES:
            return True, 'video'
        if mime_lower in AUDIO_MIMES:
            return True, 'audio'
        if mime_lower in IMAGE_MIMES:
            return True, 'image'
    
    return False, 'unknown'


def get_file_category(url: str, mime: str = None) -> str:
    """
    Get file category: 'video', 'audio', 'image', 'archive', 'document', or 'unknown'
    """
    path = urllib.parse.urlparse(url).path.lower()
    ext = os.path.splitext(path)[1]
    
    # Check by extension
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in AUDIO_EXTS:
        return 'audio'
    if ext in ARCHIVE_EXTS:
        return 'archive'
    if ext in DOCUMENT_EXTS:
        return 'document'
    
    # Check by MIME type
    if mime:
        mime_lower = mime.lower()
        if any(mime_lower.startswith(m) for m in MEDIA_TYPES):
            if mime_lower.startswith('video/'):
                return 'video'
            if mime_lower.startswith('audio/'):
                return 'audio'
            if mime_lower.startswith('image/'):
                return 'image'
    
    return 'unknown'


async def probe_content_type(url: str) -> str | None:
    """Probe URL to get Content-Type header."""
    session = await get_http_session()
    try:
        async with session.head(
            url, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=10),
            proxy=Config.PROXY
        ) as head:
            return head.headers.get("Content-Type", "").split(";")[0].strip()
    except Exception:
        return None


def needs_ffmpeg_download(url: str, mime: str) -> bool:
    """Return True if this URL must be downloaded with ffmpeg instead of aiohttp."""
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    return ext in STREAMING_EXTENSIONS or (mime or "").lower() in HLS_MIME_TYPES

async def probe_file_size(url: str) -> int | None:
    """Aggressively probe for file size using HEAD and Range GET fallback."""
    session = await get_http_session()
    # 1. Try HEAD first
    try:
        async with session.head(
            url, allow_redirects=True, 
            timeout=aiohttp.ClientTimeout(total=8),
            proxy=Config.PROXY
        ) as head:
            cl = head.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
    except Exception:
        pass
        
    # 2. Try GET with Range: bytes=0-0 (Fallback for servers blocking HEAD)
    try:
        headers = {"Range": "bytes=0-0"}
        async with session.get(
            url, allow_redirects=True,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=8),
            proxy=Config.PROXY
        ) as resp:
            # Look at Content-Range header: bytes 0-0/TOTAL_SIZE
            cr = resp.headers.get("Content-Range")
            if cr and "/" in cr:
                total = cr.split("/")[-1]
                if total.isdigit():
                    return int(total)
            # Some servers might ignore Range and return 200 with whole file length
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
    except Exception:
        pass
    return None


async def resolve_url(url: str) -> str:
    """Resolve redirecting URLs (like reddit shortlinks) and bypass Twitter NSFW blocks."""
    # 1. Resolve Reddit short links
    if "redd.it" in url or "/s/" in url.lower():
        for _ in range(2): # 2 retries
            try:
                session = await get_http_session()
                async with session.head(
                    url, 
                    allow_redirects=True, 
                    timeout=10, 
                    proxy=Config.PROXY
                ) as resp:
                    url = str(resp.url)
                    break
            except Exception:
                await asyncio.sleep(1)

    # 2. Extract Twitter direct media URL if it's a tweet link
    if any(domain in url.lower() for domain in ["twitter.com", "x.com", "t.co"]):
        # Resolve t.co first if necessary
        if "t.co" in url.lower():
            for _ in range(2):
                try:
                    session = await get_http_session()
                    async with session.head(
                        url, 
                        allow_redirects=True, 
                        timeout=10, 
                        proxy=Config.PROXY
                    ) as resp:
                        url = str(resp.url)
                        break
                except Exception:
                    await asyncio.sleep(1)
                
        # Now Check for twitter.com / x.com and try vxtwitter API
        match = re.search(r'(?:twitter\.com|x\.com)/(?:[^/]+/status/|status/|status/|/)([0-9]+)', url, re.IGNORECASE)
        if match:
            tweet_id = match.group(1)
            api_url = f"https://api.vxtwitter.com/x/status/{tweet_id}"
            for _ in range(2):
                try:
                    session = await get_http_session()
                    async with session.get(api_url, timeout=10, proxy=Config.PROXY) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            media_urls = data.get("mediaURLs", [])
                            if media_urls:
                                # We return the direct raw video/image URL instead of the twitter page!
                                return media_urls[0]
                    break
                except Exception:
                    await asyncio.sleep(1)

    return url


def smart_output_name(filename: str) -> str:
    """
    Remap known streaming extensions to the proper container extension.
    e.g. 'stream.m3u8' → 'stream.mp4'
    """
    stem, ext = os.path.splitext(filename)
    return stem + STREAMING_EXTENSIONS.get(ext.lower(), ext)

# ── yt-dlp integration ───────────────────────────────────────────────────────

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False

_YTDLP_EXTRACTORS = None

def _get_ytdlp_extractors():
    """Lazy-load and cache the yt-dlp extractors (excluding GenericIE fallback)."""
    global _YTDLP_EXTRACTORS
    if _YTDLP_EXTRACTORS is None:
        try:
            from yt_dlp.extractor import gen_extractors
            _YTDLP_EXTRACTORS = [e for e in gen_extractors() if e.IE_NAME != 'generic']
        except Exception as e:
            Config.LOGGER.error(f"Failed to load yt-dlp extractors: {e}")
            _YTDLP_EXTRACTORS = []
    return _YTDLP_EXTRACTORS

# Domains where yt-dlp should be used directly. Additions here bypass dynamic extract checks and Regex strictness.
YTDLP_DOMAINS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "instagram.com",
    "threads.com", "threads.net",
    "twitter.com", "x.com", "t.co",
    "tiktok.com", "vm.tiktok.com",
    "facebook.com", "fb.watch", "fb.com",
    "reddit.com", "v.redd.it", "redd.it",
    "dailymotion.com", "dai.ly",
    "vimeo.com",
    "twitch.tv", "clips.twitch.tv",
    "soundcloud.com",
    "bilibili.com", "b23.tv",
    "pinterest.com", "pin.it",
    "streamable.com",
    "rumble.com",
    "odysee.com",
    "bitchute.com",
    "mixcloud.com",
    "pornhub.com", "phncdn.com",
    "xvideos.com", "xvideos.es",
    "xhamster.com", "xhamster.desi",
    "xnxx.com",
    "eporner.com",
    "thumbzilla.com",
    "tube8.com",
    "youporn.com",
    "hqporner.com",
    "pornone.com",
    "thumbzillaporn.com",
    "thefamilysextube.com",
    "anysex.com",
    "hqsextube.com",
    "h2porn.com",
    "befuck.com",
    "sexvid.xxx",
    "eporner.com",
}

# Domains where cobalt API can be used as an alternative/fallback
COBALT_DOMAINS = {
    "youtube.com", "youtu.be",
    "instagram.com", "ddinstagram.com", "i.instagram.com",
    "reddit.com", "v.redd.it", "redd.it",
    "facebook.com", "fb.watch", "fb.com",
    "twitter.com", "x.com",
    "tiktok.com",
}


def is_ytdlp_url(url: str) -> bool:
    """Return True if the URL belongs to a yt-dlp-supported platform dynamically."""
    if not YTDLP_AVAILABLE:
        return False
    try:
        host = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
        # Step 1: Check Hardcoded fallback domains (handles shortened links like `t.co` and `fb.com/share`)
        if any(host == d or host.endswith("." + d) for d in YTDLP_DOMAINS):
            return True
        # Step 2: Dynamically query all yt-dlp extractors natively supported
        extractors = _get_ytdlp_extractors()
        return any(e.suitable(url) for e in extractors)
    except Exception:
        return False


def is_cobalt_url(url: str) -> bool:
    """Return True if the URL can be handled by cobalt API as a fallback."""
    if not Config.COBALT_API_URL:
        return False
    try:
        host = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in COBALT_DOMAINS)
    except Exception:
        return False


async def fetch_link_api(url: str) -> str | None:
    """
    Call the link-api GET /grab endpoint to extract a direct download URL.
    Uses local extractor if LINK_API_URL is not set, otherwise uses external API.
    Returns the best direct URL string, or None if unavailable.
    """
    # Try local extractor first (works without external API)
    try:
        from plugins.helper.extractor import extract_links
        Config.LOGGER.info(f"Using local extractor for link grab: {url}")
        result = await extract_links(url, use_browser=True, timeout=45)
        if result:
            best = result.get("best_link")
            if best:
                Config.LOGGER.info(f"Local extractor best_link: {best[:80]}")
                return best
            links = result.get("links", [])
            if links:
                def _score(link: dict) -> tuple:
                    has_av = link.get("has_video", False) and link.get("has_audio", False)
                    is_mp4 = link.get("stream_type", "") == "mp4"
                    height = link.get("height") or 0
                    return (has_av, is_mp4, height)
                links_sorted = sorted(links, key=_score, reverse=True)
                chosen = links_sorted[0].get("url")
                Config.LOGGER.info(f"Local extractor chose: {str(chosen)[:80]}")
                return chosen
    except Exception as e:
        Config.LOGGER.warning(f"Local extractor failed for {url}: {e}")
    
    # Fallback to external API if configured
    if not Config.LINK_API_URL:
        return None

    # Use GET with query params — simpler than POST and avoids any body encoding issues
    params = {"url": url, "use_browser": "true", "timeout": "30"}
    api_url = Config.LINK_API_URL.rstrip("/") + "/grab"
    session = await get_http_session()
    try:
        async with session.get(
            api_url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=50),  # Playwright render can be slow
        ) as resp:
            if resp.status != 200:
                # Log the detail body so we know WHY it failed
                try:
                    err_body = await resp.json()
                    detail = err_body.get("detail", resp.reason)
                except Exception:
                    detail = await resp.text()
                Config.LOGGER.warning(
                    f"link-api returned HTTP {resp.status} for {url}: {detail}"
                )
                return None

            data = await resp.json()

            # Prefer best_link from the API recommendation
            best = data.get("best_link")
            if best:
                Config.LOGGER.info(f"link-api best_link: {best[:80]}")
                return best

            # If no best_link, pick from links[] — prefer MP4 with audio+video at highest resolution
            links = data.get("links", [])
            if not links:
                Config.LOGGER.warning(f"link-api returned 0 links for {url}")
                return None

            def _score(link: dict) -> tuple:
                has_av = link.get("has_video", False) and link.get("has_audio", False)
                is_mp4 = link.get("stream_type", "") == "mp4"
                height = link.get("height") or 0
                return (has_av, is_mp4, height)

            links_sorted = sorted(links, key=_score, reverse=True)
            chosen = links_sorted[0].get("url")
            Config.LOGGER.info(f"link-api chose: {str(chosen)[:80]}")
            return chosen

    except Exception as e:
        Config.LOGGER.warning(f"link-api fetch failed for {url}: {e}")
        return None


def cancel_button(user_id: int) -> InlineKeyboardMarkup:
    """Build a simple cancel button markup."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{user_id}")
    ]])


async def _safe_edit(msg, text: str, reply_markup=None):
    """Edit a Telegram message, logging errors for debugging."""
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        Config.LOGGER.warning(f"Failed to edit message: {e}")


async def external_extract_ytdlp(url: str) -> dict | None:
    """
    Try local extractor first, then external API as fallback.
    """
    # Try local extractor first if LINK_API_URL is not set (meaning using local mode)
    if not Config.LINK_API_URL:
        try:
            from plugins.helper.extractor import extract_raw_ytdlp
            Config.LOGGER.info(f"Using local extractor for: {url}")
            result = await extract_raw_ytdlp(url)
            if result and result.get("formats"):
                Config.LOGGER.info(f"Successfully extracted metadata via local extractor: {url}")
                return result
        except Exception as e:
            Config.LOGGER.error(f"Local extractor failed: {e}")
            return None
    
    if not Config.LINK_API_URL:
        return None
    
    api_url = f"{Config.LINK_API_URL.rstrip('/')}/extract"
    try:
        session = await get_http_session()
        # High timeout (120s) because the external API uses WARP + Cold Boot
        async with session.post(api_url, json={"url": url}, timeout=120) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "error" in data and not data.get("formats") and not data.get("entries"):
                    Config.LOGGER.error(f"External yt-dlp API returned error: {data['error']}")
                    return None
                
                # If it's a playlist, return it as is or resolve to first entry if caller expects single video
                Config.LOGGER.info(f"Successfully extracted metadata via external yt-dlp API: {url}")
                return data
            else:
                Config.LOGGER.error(f"External yt-dlp API failed with status {resp.status}")
    except Exception as e:
        Config.LOGGER.error(f"External yt-dlp API exception: {e}")
    return None


async def fetch_ytdlp_title(url: str) -> str | None:
    """
    Extract the video title from yt-dlp (no download).
    Returns a clean filename like 'My Video Title.mp4', or None on failure.
    """
    # Block YouTube - title extraction disabled
    # SECONDARY FALLBACK: Try external API (link-api) if local yt-dlp fails
    if Config.LINK_API_URL:
        Config.LOGGER.info(f"Fallback title extraction via link-api for: {url}")
        try:
            info = await external_extract_ytdlp(url)
            if info:
                # Handle Playlists: If info has 'entries', take the first one
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                
                title = info.get("title") or info.get("id") or "video"
                title = re.sub(r'[\\/*?"<>|:\n\r\t]', "_", title).strip()
                ext = info.get("ext") or "mp4"
                return f"{title[:80]}.{ext}"
        except Exception:
            pass

    loop = asyncio.get_running_loop()

    def _fetch():
        try:
            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "format_sort": ["res", "vbr", "tbr", "fps"],
                "force_ipv4": True, # Common fix for Connection Reset on VPS
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web", "creator"],
                    },
                    "youtubepot-bgutilhttp": {
                        "base_url": ["http://localhost:4416"],
                    }
                }
            }
            if Config.COOKIES_FILE and os.path.exists(Config.COOKIES_FILE):
                opts["cookiefile"] = Config.COOKIES_FILE
            if Config.PROXY:
                opts["proxy"] = Config.PROXY

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Handle Playlists: If info has 'entries', take the first one
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                    Config.LOGGER.info(f"Detected playlist, resolving to first entry: {info.get('title')}")

                title = info.get("title") or info.get("id") or "video"
                title = re.sub(r'[\\/*?"<>|:\n\r\t]', "_", title).strip()
                ext = info.get("ext") or "mp4"
                return f"{title[:80]}.{ext}"
        except Exception:
            return None

    res = await loop.run_in_executor(None, _fetch)
    
    # Fallback: If local yt-dlp failed, try local extractor
    if not res:
        Config.LOGGER.info(f"Local yt-dlp title failed, trying local extractor for: {url}")
        try:
            from plugins.helper.extractor import extract_raw_ytdlp
            info = await extract_raw_ytdlp(url)
            if info:
                title = info.get("title") or "video"
                title = re.sub(r'[\\/*?"<>|:\n\r\t]', "_", title).strip()
                ext = "mp4"
                return f"{title[:80]}.{ext}"
        except Exception as e:
            Config.LOGGER.error(f"Local extractor title fallback failed: {e}")
    
    return res


async def fetch_http_filename(url: str, default_name: str = "downloaded_file") -> str:
    """
    Probe a direct URL with a HEAD request to extract the true filename from Content-Disposition
    or guess the extension from the Content-Type.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        session = await get_http_session()
        async with session.head(
            url, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=10),
            proxy=Config.PROXY
        ) as head:
            mime = head.headers.get("Content-Type", "").split(";")[0].strip()
            cd = head.headers.get("Content-Disposition", "")
            # Check server-provided exact filename
            cd_match = re.search(r'filename="?([^"]+)"?', cd)
            if cd_match:
                return smart_output_name(cd_match.group(1))

            # If no Content-Disposition, check if the parsed URL name lacks an extension
            parsed_name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path.rstrip("/")))
            base_name = parsed_name if parsed_name else default_name
            
            if not os.path.splitext(base_name)[1]:
                ext = mimetypes.guess_extension(mime)
                if ext:
                    if ext == '.jpe': ext = '.jpg'
                    base_name += ext
                    
            return smart_output_name(base_name)
    except Exception:
        # Fallback to standard URL parsing
        parsed = urllib.parse.urlparse(url)
        name = os.path.basename(parsed.path.rstrip("/"))
        return smart_output_name(urllib.parse.unquote(name) if name else default_name)


async def get_best_filename(url: str, default_name: str = "downloaded_file") -> str:
    """
    Universally determine the best filename for any given URL.
    Routes to yt-dlp native extraction first, falling back to HTTP header sniffing for direct routes.
    """
    if is_ytdlp_url(url):
        ytdlp_title = await fetch_ytdlp_title(url)
        if ytdlp_title:
            return ytdlp_title
        # Even if it's a yt-dlp URL, if the title extraction fails (like on Pinterest), fall back
    
    # If it's a cobalt URL, Cobalt handles social media links so HTTP probes usually just return HTML.
    # So we don't bother probing Cobalt URLs, just return the parsed stem + default .mp4
    if is_cobalt_url(url):
        parsed = urllib.parse.urlparse(url)
        name = os.path.basename(parsed.path.rstrip("/"))
        base_name = urllib.parse.unquote(name) if name else default_name
        if not os.path.splitext(base_name)[1]:
            base_name += ".mp4"
        return smart_output_name(base_name)

    return await fetch_http_filename(url, default_name)


async def fetch_ytdlp_formats(url: str) -> dict:
    """
    Fetch available video formats from yt-dlp.
    Returns: {"formats": list[dict], "title": str}
    """
    if not YTDLP_AVAILABLE:
        return {"formats": [], "title": ""}

    # For YouTube: try custom YouTube API first
    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        Config.LOGGER.info(f"YouTube format extraction: {url}, API_URL={Config.YOUTUBE_API_URL}")
        
        # Try custom YouTube API if configured
        if Config.YOUTUBE_API_URL:
            Config.LOGGER.info("Using YouTube API for format extraction")
            try:
                result = await fetch_youtube_formats(url)
                if result and result.get("formats"):
                    Config.LOGGER.info(f"YouTube API returned {len(result['formats'])} formats")
                    return result
                else:
                    Config.LOGGER.warning("YouTube API returned empty formats")
            except Exception as e:
                Config.LOGGER.warning(f"YouTube API failed: {e}")
        else:
            Config.LOGGER.info("YOUTUBE_API_URL not set, using fallback")
        
        # Fall back to external extract
        info = await external_extract_ytdlp(url)
        if info and info.get("formats"):
            # Handle Playlists: If info has 'entries', take the first one
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            formats = info.get("formats", [])
            title = info.get("title", "video")

            best_audio_size = 0
            for f in formats:
                if f.get("vcodec") == "none" and f.get("acodec") != "none":
                    size = f.get("filesize") or f.get("filesize_approx") or 0
                    if size > best_audio_size:
                        best_audio_size = size

            available = {}
            for f in formats:
                height = f.get("height")
                if height and f.get("vcodec") != "none":
                    res = f"{height}p"
                    available[res] = f

            format_results = []
            sorted_res = sorted(
                available.keys(),
                key=lambda x: int(re.search(r'(\d+)p', x).group(1)) if re.search(r'(\d+)p', x) else 0,
                reverse=True
            )

            for res in sorted_res:
                f = available[res]
                size = f.get("filesize") or f.get("filesize_approx")
                has_audio = f.get("acodec") != "none"
                
                # Estimate size if missing using bitrate and duration
                if size is None:
                    tbr = f.get("tbr")
                    duration = info.get("duration")
                    if tbr and duration:
                        size = int((tbr * 1024 / 8) * duration)

                if not has_audio and size is not None and best_audio_size > 0:
                    size += best_audio_size

                format_results.append({
                    "format_id": f["format_id"],
                    "resolution": res,
                    "ext": f.get("ext", "mp4"),
                    "filesize": size,
                    "has_audio": has_audio,
                    "bitrate": f.get("tbr") or f.get("vbr") or 0,
                    "url": f.get("url")
                })

            if format_results:
                # Probe for accurate filesizes
                session = await get_http_session()
                for f_dict in format_results:
                    if f_dict.get("filesize") is None and f_dict.get("url"):
                        try:
                            async with session.head(
                                f_dict["url"], allow_redirects=True, 
                                timeout=aiohttp.ClientTimeout(total=5),
                                proxy=Config.PROXY
                            ) as head:
                                cl = head.headers.get("Content-Length")
                                if cl and cl.isdigit():
                                    f_dict["filesize"] = int(cl)
                        except Exception:
                            pass
                    if "url" in f_dict:
                        del f_dict["url"]
                
                return {"formats": format_results, "title": title}

    # For other sites: try local yt-dlp first
    loop = asyncio.get_running_loop()

    def _fetch():
        try:
            opts = {
                "quiet": True,
                "no_warnings": True,
                "format_sort": ["res", "vbr", "tbr", "fps"],
                "force_ipv4": True,
                "nocheckcertificate": True,
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "extractor_args": {
                    "youtube": {"player_client": ["web", "creator"]},
                    "youtubepot-bgutilhttp": {"base_url": ["http://localhost:4416"]}
                }
            }

            if Config.COOKIES_FILE and os.path.exists(Config.COOKIES_FILE):
                opts["cookiefile"] = Config.COOKIES_FILE
            if Config.PROXY:
                opts["proxy"] = Config.PROXY
            
            # NSFW / PornHub tweaks
            if "pornhub.com" in url:
                opts["referer"] = "https://www.pornhub.com/"
                opts["geo_bypass"] = True
                opts["socket_timeout"] = 20
                opts["extractor_args"]["pornhub"] = {'prefer_formats': 'mp4'}

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Handle Playlists: If info has 'entries', take the first one
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                    Config.LOGGER.info(f"Detected playlist in format extraction, resolving to first entry: {info.get('title')}")

                formats = info.get("formats", [])
                title = info.get("title", "video")
                
                # 1. Calculate best audio size for estimation
                best_audio_size = 0
                for f in formats:
                    if f.get("vcodec") == "none" and f.get("acodec") != "none":
                        size = f.get("filesize") or f.get("filesize_approx") or 0
                        if size > best_audio_size:
                            best_audio_size = size

                # 2. Extract and normalize video formats with CDN quality detection
                available = {}
                audio_sizes = {}  # Track audio sizes per resolution
                
                # First pass: collect audio sizes for each resolution
                for f in formats:
                    if f.get("vcodec") == "none" and f.get("acodec") != "none":
                        size = f.get("filesize") or f.get("filesize_approx") or 0
                        tbr = f.get("tbr") or 0
                        # Try to estimate from bitrate if no filesize
                        if size == 0 and tbr:
                            duration = info.get("duration") or 0
                            if duration:
                                size = int((tbr * 1024 / 8) * duration)
                        # Group by approximate bitrate range
                        bitrate_range = (tbr // 64) * 64 if tbr else 0
                        if bitrate_range not in audio_sizes or size > audio_sizes.get(bitrate_range, 0):
                            audio_sizes[bitrate_range] = size
                
                # Get the best audio size available
                best_audio_size = max(audio_sizes.values()) if audio_sizes else 0
                
                for f in formats:
                    if f.get("vcodec") == "none":
                        continue
                    
                    # Extract dimensions
                    w = f.get("width") or 0
                    h = f.get("height") or 0
                    
                    # Parse from resolution string fallback
                    if not (w and h):
                        res_str = f.get("resolution") or f.get("format_id") or ""
                        m = re.search(r'(\d+)x(\d+)', res_str)
                        if m:
                            w = w or int(m.group(1))
                            h = h or int(m.group(2))
                    
                    # Handle legacy Facebook tags and other extractors
                    if not h:
                        fid = str(f.get("format_id", "")).lower()
                        if fid == "hd": h = 720
                        elif fid == "sd": h = 360
                        elif "1080" in fid: h = 1080
                        elif "720" in fid: h = 720
                        elif "480" in fid: h = 480
                        elif "360" in fid: h = 360
                    
                    if h:
                        # Resolution Label: Use min(w, h) for vertical videos
                        label_res = min(w, h) if (w and h) else h
                        res_key = f"{label_res}p"
                        
                        # Calculate quality score: resolution * 1000 + bitrate
                        # This prioritizes higher resolution but breaks ties with bitrate
                        tbr = f.get("tbr") or f.get("vbr") or 0
                        quality_score = (label_res * 1000) + tbr
                        
                        # Check if this is a combined (video+audio) format
                        has_audio = f.get("acodec") != "none"
                        
                        # Selection Strategy:
                        # a. Prefer formats WITH audio (merged) - combined formats are better
                        # b. Higher bitrate within same resolution = higher quality CDN
                        if res_key not in available:
                            available[res_key] = (f, quality_score, has_audio)
                        else:
                            curr_f, curr_score, curr_has_audio = available[res_key]
                            new_has_audio = has_audio
                            curr_has_audio = curr_has_audio
                            
                            # If new has audio and current doesn't, prefer new
                            if new_has_audio and not curr_has_audio:
                                available[res_key] = (f, quality_score, has_audio)
                            elif not new_has_audio and curr_has_audio:
                                continue
                            else:
                                # Both have audio or both don't: pick higher quality (bitrate)
                                if quality_score > curr_score:
                                    available[res_key] = (f, quality_score, has_audio)
                
                # 3. Build result list
                results = []
                sorted_res = sorted(
                    available.keys(), 
                    key=lambda x: int(re.search(r'(\d+)p', x).group(1)) if re.search(r'(\d+)p', x) else 0, 
                    reverse=True
                )
                
                for res in sorted_res:
                    f, _, _ = available[res]
                    size = f.get("filesize") or f.get("filesize_approx")
                    
                    # Estimation
                    if size is None:
                        tbr = f.get("tbr")
                        duration = info.get("duration")
                        if tbr and duration:
                            size = int((tbr * 1024 / 8) * duration)

                    # Add audio if video-only
                    if f.get("acodec") == "none" and size is not None and best_audio_size > 0:
                        size += best_audio_size
                        
                    results.append({
                        "format_id": f["format_id"],
                        "resolution": res,
                        "ext": f.get("ext", "mp4"),
                        "filesize": size,
                        "has_audio": f.get("acodec") != "none",
                        "bitrate": f.get("tbr") or f.get("vbr") or 0,
                        "url": f.get("url")
                    })
                
                return {"formats": results, "title": title}
        except Exception as e:
            Config.LOGGER.error(f"Error fetching formats for {url}: {e}")
            return {"formats": [], "title": ""}

    res = await loop.run_in_executor(None, _fetch)
    
    # SECONDARY FALLBACK: If local yt-dlp failed, try local extractor
    if not res or not res.get("formats"):
        Config.LOGGER.info(f"Local yt-dlp failed, trying local extractor for: {url}")
        try:
            from plugins.helper.extractor import extract_raw_ytdlp
            info = await extract_raw_ytdlp(url)
            if info and info.get("formats"):
                formats = []
                for f in info.get("formats", []):
                    height = f.get("height") or 720
                    formats.append({
                        "format_id": f.get("format_id", "browser"),
                        "resolution": f"{height}p",
                        "ext": f.get("ext", "mp4"),
                        "filesize": f.get("filesize"),
                        "has_audio": f.get("acodec") != "none",
                        "bitrate": 0,
                        "url": f.get("url")
                    })
                if formats:
                    res = {"formats": formats, "title": info.get("title", "video")}
                    Config.LOGGER.info(f"Local extractor returned {len(formats)} formats for: {url}")
        except Exception as e:
            Config.LOGGER.error(f"Local extractor fallback failed: {e}")
    
    # TERTIARY FALLBACK: Try external API if configured
    if (not res or not res.get("formats")) and Config.LINK_API_URL:
        Config.LOGGER.info(f"Fallback format extraction via link-api for: {url}")
        info = await external_extract_ytdlp(url)
        if info:
            # Handle Playlists: If info has 'entries', take the first one
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            formats = info.get("formats", [])
            title = info.get("title", "video")
            
            format_results = []
            for f in formats:
                height = f.get("height")
                if height and f.get("vcodec") != "none":
                    size = f.get("filesize") or f.get("filesize_approx")
                    has_audio = f.get("acodec") != "none"
                    format_results.append({
                        "format_id": f["format_id"],
                        "resolution": f"{height}p",
                        "ext": f.get("ext", "mp4"),
                        "filesize": size,
                        "has_audio": has_audio,
                        "bitrate": f.get("tbr") or f.get("vbr") or 0,
                        "url": f.get("url")
                    })
            if format_results:
                res = {"formats": format_results, "title": title}

    # ── Final safety probe for missing filesizes ─────────────────────────────
    if res and res.get("formats"):
        session = await get_http_session()
        for f_dict in res["formats"]:
            if f_dict.get("filesize") is None and f_dict.get("url"):
                f_dict["filesize"] = await probe_file_size(f_dict["url"])
            # Remove temporary URL before sending to client
            if "url" in f_dict:
                del f_dict["url"]

    # Fallback: If local yt-dlp failed or returned no formats, try local extractor
    if not res or not res.get("formats"):
        Config.LOGGER.info(f"Local yt-dlp failed, trying local extractor for: {url}")
        try:
            from plugins.helper.extractor import extract_raw_ytdlp
            info = await extract_raw_ytdlp(url)
            if info and info.get("formats"):
                # Transform extractor formats to match expected format
                formats = []
                for f in info.get("formats", []):
                    height = f.get("height") or 720
                    formats.append({
                        "format_id": f.get("format_id", "browser"),
                        "resolution": f"{height}p",
                        "ext": f.get("ext", "mp4"),
                        "filesize": f.get("filesize"),
                        "has_audio": f.get("acodec") != "none",
                        "bitrate": 0,
                        "url": f.get("url")
                    })
                title = info.get("title", "video")
                res = {"formats": formats, "title": title}
                Config.LOGGER.info(f"Local extractor returned {len(formats)} formats for: {url}")
        except Exception as e:
            Config.LOGGER.error(f"Local extractor fallback failed: {e}")

    return res

async def check_ffmpeg() -> bool:
    """Check if ffmpeg is available."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _get_ffmpeg_bin(), "-version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception:
        return False


async def download_ytdlp(
    url: str,
    filename: str,
    progress_msg,
    start_time_ref: list,
    user_id: int,
    format_id: str = None,
    cancel_ref: list = None,
) -> tuple[str, str]:
    """
    Download content using yt-dlp with live progress.
    Uses the user-supplied filename as the output stem.
    If format_id is provided, it attempts to download that specific format.
    Pass cancel_ref=[False] to safely abort via asyncio.
    Returns (file_path, mime_type).
    """
    start_time_ref[0] = time.time()
    loop = asyncio.get_running_loop()
    last_edit = [start_time_ref[0]]

    out_dir = Config.DOWNLOAD_LOCATION
    os.makedirs(out_dir, exist_ok=True)
    
    Config.LOGGER.info(f"download_ytdlp started for {user_id}.")

    # Build a safe output stem from the user-chosen filename
    # Shorten to 80 chars to avoid OS length limits (e.g. for long Facebook titles)
    safe_stem = re.sub(r'[\\/*?"<>|:]', "_", os.path.splitext(filename)[0])[:80]
    if not safe_stem:
        safe_stem = "video_file"
    outtmpl = os.path.join(out_dir, f"{safe_stem}.%(ext)s")

    def _progress_hook(d: dict):
        if cancel_ref and cancel_ref[0]:
            raise asyncio.CancelledError("Download cancelled by user.")

        done = d.get("downloaded_bytes", 0) or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or d.get("filesize") or d.get("filesize_approx") or 0
        speed = d.get("speed") or 0
        eta = d.get("eta") or 0
        percent = (done / total * 100) if total else 0

        display_percent = max(1, round(percent, 1))
        if display_percent >= 100:
            display_percent = 99.5

        # Update Telegram message
        now = time.time()
        if d["status"] == "downloading" and now - last_edit[0] >= PROGRESS_UPDATE_DELAY:
            last_edit[0] = now
            bar = progress_bar(done, total) if total else "░" * 12
            pct = f"{done / total * 100:.1f}%" if total else "…"
            text = (
                f"📥 **Downloading Media…**\n\n"
                f"📁 **Name:** `{os.path.basename(outtmpl)}`\n"
                f"[{bar}] {pct}\n"
                f"**Done:** {humanbytes(done)}"
                + (f" / {humanbytes(total)}" if total else "")
                + (f"\n**Speed:** {humanbytes(speed)}/s" if speed else "")
                + (f"\n**ETA:** {time_formatter(eta)}" if eta else "")
            )
            asyncio.run_coroutine_threadsafe(
                _safe_edit(progress_msg, text, reply_markup=cancel_button(user_id)),
                loop
            )

    # ── Build format string based on ffmpeg availability ─────────────────────
    ffmpeg_available = await check_ffmpeg()

    if format_id == "best":
        # User requested absolute max quality (unrestricted resolution)
        if ffmpeg_available:
            fmt = "bestvideo+bestaudio/best"
        else:
            fmt = "best"
    elif format_id:
        # If user picked a specific resolution, we try to get that video + best audio
        # or just that specific format if it's already merged.
        if ffmpeg_available:
            fmt = f"{format_id}+bestaudio/{format_id}/best"
        else:
            fmt = f"{format_id}/best"
    elif ffmpeg_available:
        # ffmpeg found → prefer best quality separate streams, merge to mp4
        fmt = (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[height<=1080]+bestaudio"
            "/best[height<=1080]/best"
        )
    else:
        # NO ffmpeg → only pick formats that are already a single file (no merge needed)
        fmt = (
            "best[height<=1080][ext=mp4]"
            "/best[height<=1080]"
            "/best"
        )

    ydl_opts = {
        "format": fmt,
        "format_sort": [
            "res", "vbr", "tbr", "fps", "size"
        ],
        "outtmpl": outtmpl,
        "progress_hooks": [_progress_hook],
        "quiet": True,
        "no_warnings": True,
        "force_ipv4": True,
        "nocheckcertificate": True,
        "cookiefile": Config.COOKIES_FILE,
        "merge_output_format": "mp4",
        "overwrites": True,
        "noplaylist": True,
        "max_filesize": Config.MAX_FILE_SIZE,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "concurrent_fragment_downloads": 10, # Reduced for improved stability on low-resource hosts
        "hls_prefer_native": True,          # Native HLS allows better progress reporting
        "cachedir": False,                   # Disable cache to avoid path/permission issues on Koyeb
        "trim_file_name": 100,              # Prevent Errno 2 by ensuring total path length stays safe
        "retries": 15,
        "fragment_retries": 15,
        "buffersize": 1048576,               # 1MB Buffer for speed
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "creator"],
            },
            "youtubepot-bgutilhttp": {
                "base_url": ["http://localhost:4416"],
            }
        },
        "postprocessor_args": {
            "merger": [
                "-timeout", "10000000",
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5"
            ],
            "ffmpeg": [
                "-timeout", "10000000",
                "-reconnect", "1",
                "-reconnect_at_eof", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5"
            ]
        }
    }

    # Additional logic for NSFW/Blocked sites
    if "pornhub.com" in url.lower():
        ydl_opts["http_headers"] = {"Referer": "https://www.pornhub.com/"}
        ydl_opts["extractor_args"]["pornhub"] = {'prefer_formats': 'mp4'}

    # Set ffmpeg_location to the DIRECTORY, not the binary path
    ffmpeg_dir = _get_ffmpeg_dir()
    if ffmpeg_dir:
        ydl_opts["ffmpeg_location"] = ffmpeg_dir

    if Config.COOKIES_FILE and os.path.exists(Config.COOKIES_FILE):
        ydl_opts["cookiefile"] = Config.COOKIES_FILE

    if Config.PROXY:
        ydl_opts["proxy"] = Config.PROXY

    # Platform specific tweaks
    if "reddit.com" in url or "v.redd.it" in url:
        ydl_opts["referer"] = "https://www.reddit.com/"
    elif "pornhub.com" in url:
        ydl_opts["referer"] = "https://www.pornhub.com/"
        ydl_opts["geo_bypass"] = True
        ydl_opts["socket_timeout"] = 30
        ydl_opts["extractor_args"] = {'pornhub': {'prefer_formats': 'mp4'}}

    async def _run_async() -> str:
        try:
            # PRIMARY FOR YOUTUBE: Try external API first during download exploration too
            cached_info = None
            if "youtube.com" in url or "youtu.be" in url:
                Config.LOGGER.info(f"Primary YouTube download extraction via external API: {url}")
                cached_info = await external_extract_ytdlp(url)

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # 1. Extract metadata only
                if cached_info:
                    info = cached_info
                    # If external API provides a specific format for the requested resolution, try to re-process it locally
                    # Note: process_info will handle the download.
                else:
                    try:
                        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                    except Exception as e:
                        # FALLBACK: If local extraction fails for YouTube during download phase
                        if "youtube.com" in url or "youtu.be" in url:
                            Config.LOGGER.info(f"Local download-phase extraction failed for YouTube. Trying external API: {url}")
                            info = await external_extract_ytdlp(url)
                            if not info:
                                raise e 
                        else:
                            raise e

                # Use safe_stem to build path (we prefer the user's filename)
                ext = info.get("ext", "mp4")
                target_path = os.path.join(out_dir, f"{safe_stem}.{ext}")
                
                # 2. Identify if handover to aria2c is possible (single direct file)
                req_formats = info.get("requested_formats")
                is_single = not req_formats or len(req_formats) == 1
                protocol = info.get("protocol", "")
                
                extractor = info.get("extractor_key", "").lower()
                tricky_extractors = ["facebook", "instagram", "twitter", "x", "tiktok"]
                is_tricky = any(t in extractor for t in tricky_extractors)
                
                is_direct = "http" in protocol and "m3u8" not in protocol and "dash" not in protocol and not is_tricky

                downloaded_path = None
                if is_single and is_direct:
                    Config.LOGGER.info(f"Handing off direct URL to aria2c for max speed: {url}")
                    headers = info.get("http_headers", {})
                    downloaded_path = await _download_aria2c(
                        info["url"], target_path, progress_msg, start_time_ref, user_id, 
                        cancel_ref=cancel_ref, 
                        headers=headers
                    )
                else:
                    # 3. Fallback: Use yt-dlp native downloader
                    Config.LOGGER.info(f"Using native yt-dlp downloader for complex stream/merge: {url}")
                    await loop.run_in_executor(None, lambda: ydl.process_info(info))
                    
                    # Determine output path
                    mp4_path = os.path.join(out_dir, f"{safe_stem}.mp4")
                    if os.path.exists(mp4_path):
                        downloaded_path = mp4_path
                    else:
                        candidates = sorted(
                            [f for f in os.listdir(out_dir) if f.startswith(safe_stem)],
                            key=lambda f: os.path.getsize(os.path.join(out_dir, f)),
                            reverse=True,
                        )
                        if candidates:
                            downloaded_path = os.path.join(out_dir, candidates[0])
                
                if not downloaded_path or not os.path.exists(downloaded_path):
                    raise FileNotFoundError("Error: output file not found after download")

                # CRITICAL: Fix 0B file issue
                if os.path.getsize(downloaded_path) == 0:
                    try:
                        os.remove(downloaded_path)
                    except:
                        pass
                    raise ValueError("Downloaded file is empty (0 bytes). The download probably failed silently or was blocked.")

                return downloaded_path

        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            Config.LOGGER.error(f"yt-dlp/aria2c critical error for {url}: {e}")
            raise

    file_path = await _run_async()
    mime = mimetypes.guess_type(file_path)[0] or "video/mp4"
    return file_path, mime


# ── Cobalt API fallback ──────────────────────────────────────────────────────

async def download_cobalt(
    url: str,
    filename: str,
    progress_msg,
    start_time_ref: list,
    user_id: int,
    cancel_ref: list = None,
) -> tuple[str, str]:
    """
    Download content using the cobalt API (fallback for Instagram/Pinterest).
    No cookies required — cobalt handles authentication independently.
    Returns (file_path, mime_type).
    """
    start_time_ref[0] = time.time()
    out_dir = Config.DOWNLOAD_LOCATION
    os.makedirs(out_dir, exist_ok=True)
    safe_stem = re.sub(r'[\\/*?"<>|:]', "_", os.path.splitext(filename)[0])[:80]

    api_url = Config.COBALT_API_URL.rstrip("/")
    payload = {
        "url": url,
        "downloadMode": "auto",
        "videoQuality": "1080",
        "filenameStyle": "basic",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "TelegramBot/1.0",
    }

    try:
        await _safe_edit(
            progress_msg,
            "📥 **Initializing Download…** ⏳\n_Please wait while we prepare your file..._",
            reply_markup=cancel_button(user_id)
        )

        session = await get_http_session()
        # Step 1: Ask cobalt for the download URL
        async with session.post(
            f"{api_url}/",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
            proxy=Config.PROXY
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise ValueError(f"Download server returned {resp.status}: {error_text[:200]}")
            data = await resp.json()

            status = data.get("status")

            if status == "error":
                error_code = data.get("error", {}).get("code", "unknown")
                raise ValueError(f"Extraction error: {error_code}")

            # Get the download URL from the response
            if status in ("tunnel", "redirect"):
                download_url_str = data.get("url")
                cobalt_filename = data.get("filename", f"{safe_stem}.mp4")
            elif status == "picker":
                # Multiple items — take the first video/photo
                picker = data.get("picker", [])
                if not picker:
                    raise ValueError("No media found to extract")
                download_url_str = picker[0].get("url")
                cobalt_filename = f"{safe_stem}.mp4"
            else:
                raise ValueError(f"Download server returned unexpected status: {status}")

            if not download_url_str:
                raise ValueError("Could not extract media URL")

            # Determine output file extension from cobalt filename
            _, ext = os.path.splitext(cobalt_filename)
            if not ext:
                ext = ".mp4"
            out_path = os.path.join(out_dir, f"{safe_stem}{ext}")

            try:
                # Step 2: Download extremely fast via aria2c using the Cobalt proxy URL
                await _safe_edit(progress_msg, "📥 **Extracting Media…** ⚙️", reply_markup=cancel_button(user_id))
                
                await _download_aria2c(download_url_str, out_path, progress_msg, start_time_ref, user_id, cancel_ref=cancel_ref)

            except Exception:
                if os.path.exists(out_path):
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass
                raise

            if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                try:
                    os.remove(out_path)
                except:
                    pass
                raise ValueError("Downloaded file from secondary server is empty (0 bytes).")

            mime = mimetypes.guess_type(out_path)[0] or "video/mp4"
            return out_path, mime

    except Exception as e:
        raise ValueError(f"Download failed: {e}")


def humanbytes(size: int) -> str:
    if size is None or size < 0:
        return "Unknown"
    if not size:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            if unit in ["B", "KB"]:
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def time_formatter(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    elif minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def progress_bar(current: int, total: int, length: int = 12) -> str:
    filled = int(length * current / total) if total else 0
    bar = "█" * filled + "░" * (length - filled)
    percent = current / total * 100 if total else 0
    return f"[{bar}] {percent:.1f}%"


# ── FFprobe / FFmpeg helpers ──────────────────────────────────────────────────

async def get_video_metadata(file_path: str) -> dict:
    """
    Use ffprobe (async subprocess) to extract duration, width, height from a video.
    Returns a dict with keys: duration (int seconds), width (int), height (int).
    Falls back to zeros if ffprobe is unavailable or fails.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            _get_ffprobe_bin(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        data = json.loads(stdout)
        video_stream = next(
            (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
            None,
        )
        duration = int(float(data.get("format", {}).get("duration", 0)))
        width = int(video_stream.get("width", 0)) if video_stream else 0
        height = int(video_stream.get("height", 0)) if video_stream else 0
        return {"duration": duration, "width": width, "height": height}
    except Exception:
        return {"duration": 0, "width": 0, "height": 0}


async def generate_video_thumbnail(file_path: str, chat_id: int, duration: int = 0) -> str | None:
    """
    Extract a single frame from the video at 10% of its duration (or 1 s if unknown),
    scaled to max width 320 px, saved as JPEG.  Returns the path or None on failure.
    """
    thumb_path = os.path.join(Config.DOWNLOAD_LOCATION, f"thumb_auto_{chat_id}.jpg")
    # Pick a timestamp: 0 seconds (first frame) to avoid any seeking overhead and instantly grab the screen
    seek = 0
    try:
        proc = await asyncio.create_subprocess_exec(
            _get_ffmpeg_bin(),
            "-y",
            "-threads", "1",
            "-ss", str(seek),
            "-i", file_path,
            "-vframes", "1",
            "-vf", "scale=320:-1",
            "-q:v", "2",          # JPEG quality (2 = very high, 31 = worst)
            thumb_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=60)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception:
        pass
    return None


# ── Download helpers ──────────────────────────────────────────────────────────

async def _download_hls(url: str, out_path: str, progress_msg, start_time_ref: list, user_id: int, cancel_ref: list = None, headers: dict = None) -> str:
    """
    Use ffmpeg to download an HLS/DASH/TS stream and remux it to mp4.
    Shows elapsed-time progress (no size info available for streams).
    """
    start_time_ref[0] = time.time()
    last_edit = start_time_ref[0]

    global_start = time.time()
    last_size = 0
    last_size_time = time.time()
    
    # Build FFmpeg headers
    ffmpeg_headers = [
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept: */*",
        "Accept-Language: en-US,en;q=0.9",
        "Referer: https://www.pornhub.com/",
    ]
    
    if headers:
        for k, v in headers.items():
            if k.lower() not in ("user-agent",):
                ffmpeg_headers.append(f"{k}: {v}")
    
    # Build FFmpeg command with headers
    cmd = [
        _get_ffmpeg_bin(), "-y",
        "-timeout", "20000000",        # 20s network timeout
        "-reconnect", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
    ]
    
    # Add headers
    for h in ffmpeg_headers:
        cmd.extend(["-headers", h])
    
    cmd.extend([
        "-i", url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        out_path,
    ])
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    # Read stderr in background so pipe doesn't fill up and block ffmpeg
    stderr_chunks = []

    async def _read_stderr():
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            stderr_chunks.append(line)

    stderr_task = asyncio.create_task(_read_stderr())

    # Poll until ffmpeg finishes, editing progress every PROGRESS_UPDATE_DELAY s
    try:
        while True:
            try:
                # Wait for process to finish with a small timeout so we can update progress
                await asyncio.wait_for(proc.wait(), timeout=2.0)
                break  # process finished
            except asyncio.TimeoutError:
                pass  # still running, update progress
            
            if cancel_ref and cancel_ref[0]:
                try: proc.kill()
                except: pass
                raise asyncio.CancelledError("Upload cancelled.")

            now = time.time()
            
            # Global Timeout: 1 hour max for a single stream
            if now - global_start > 3600:
                try: proc.kill()
                except: pass
                Config.LOGGER.error(f"FFmpeg global timeout exceeded for {url}")
                raise RuntimeError("Download timed out after 60 minutes.")

            # Stale Download Detection: If file size hasn't changed for 120 seconds, kill it
            if os.path.exists(out_path):
                current_size = os.path.getsize(out_path)
                if current_size > last_size:
                    last_size = current_size
                    last_size_time = now
                elif now - last_size_time > 120:
                    try: proc.kill()
                    except: pass
                    Config.LOGGER.error(f"FFmpeg stalled (no data for 2m) for {url}")
                    raise RuntimeError("Download stalled: No data received for 2 minutes.")

            if now - last_edit >= PROGRESS_UPDATE_DELAY:
                elapsed = now - global_start
                elapsed_str = time_formatter(elapsed)
                
                # Get download speed
                speed = last_size / elapsed if elapsed > 0 else 0
                speed_str = f"{humanbytes(speed)}/s" if speed > 0 else "calculating..."
                
                try:
                    await progress_msg.edit_text(
                        f"📥 **Downloading stream…**\n\n"
                        f"⏱ Elapsed: {elapsed_str}\n"
                        f"📦 Downloaded: `{humanbytes(last_size)}`\n"
                        f"🚀 Speed: {speed_str}",
                        reply_markup=cancel_button(user_id)
                    )
                except Exception:
                    pass
                last_edit = now

        await stderr_task  # ensure stderr is fully read

        if proc.returncode != 0:
            err_log = b"".join(stderr_chunks).decode(errors="replace")
            raise RuntimeError(f"ffmpeg failed (code {proc.returncode}):\n{err_log[-500:] if err_log else 'Unknown error'}")

        return out_path
    except Exception:
        # Cleanup incomplete ffmpeg output if cancelled or failed
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass


async def download_url(url: str, filename: str, progress_msg, start_time_ref: list, user_id: int, format_id: str = None, cancel_ref: list = None):
    """
    Stream-download a URL to disk, editing progress_msg periodically.
    Returns (path, mime_type) on success or raises.
    """
    download_dir = Config.DOWNLOAD_LOCATION
    os.makedirs(download_dir, exist_ok=True)

    # Remap streaming extensions to proper container (e.g. .m3u8 → .mp4)
    filename = smart_output_name(filename)
    # Shorten to 80 chars to avoid OS length limits
    safe_name = re.sub(r'[\\/*?:"<>|]', "_", filename)[:80]
    file_path = os.path.join(download_dir, safe_name)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5"
    }

    # ── Route yt-dlp-supported platforms ─────────────────────────────────────
    if is_ytdlp_url(url):
        try:
            status_text = "📥 **Doing some black magic…** 🪄\n_(connecting to the dark side…)_"
            await progress_msg.edit_text(status_text, reply_markup=cancel_button(user_id))
        except Exception:
            pass
        try:
            res_path, res_mime = await download_ytdlp(url, filename, progress_msg, start_time_ref, user_id, format_id=format_id, cancel_ref=cancel_ref)
            
            # Final guard against 0B files leaking to uploader
            if res_path and os.path.exists(res_path) and os.path.getsize(res_path) > 0:
                return res_path, res_mime
            else:
                raise ValueError("Final yt-dlp download produced a 0B file.")

        except Exception as ytdlp_err:
            if isinstance(ytdlp_err, asyncio.CancelledError):
                raise
            
            # If yt-dlp fails and cobalt supports this URL, try cobalt as fallback
            if is_cobalt_url(url):
                Config.LOGGER.info(
                    "Initial extraction failed, trying secondary servers..."
                )
                try:
                    return await download_cobalt(url, filename, progress_msg, start_time_ref, user_id, cancel_ref=cancel_ref)
                except Exception as cobalt_err:
                    # cobalt also failed — try link-api as last resort
                    Config.LOGGER.info("Cobalt failed, trying link-api as final fallback...")
                    try:
                        link_api_url = await fetch_link_api(url)
                        if link_api_url:
                            Config.LOGGER.info(f"link-api resolved URL (fallback 1): {link_api_url[:80]}...")
                            return await download_url(
                                link_api_url, filename, progress_msg, start_time_ref, user_id, 
                                format_id=format_id, cancel_ref=cancel_ref
                            )
                    except Exception as link_api_err:
                        pass
                    # All three failed — raise combined error
                    raise ValueError(
                        f"Error 1 (yt-dlp): {ytdlp_err}\n\nError 2 (cobalt): {cobalt_err}"
                    ) from ytdlp_err
                # yt-dlp failed and cobalt doesn't support this URL.
                # Try link-api as a second attempt before giving up.
                Config.LOGGER.info("yt-dlp failed, trying link-api as fallback...")
                try:
                    link_api_url = await fetch_link_api(url)
                    if link_api_url:
                        Config.LOGGER.info(f"link-api resolved URL (fallback 2): {link_api_url[:80]}")
                        
                        # Check if the resolved URL is an HLS stream
                        is_hls_url = ".m3u8" in link_api_url.lower() or link_api_url.lower().endswith("/m3u8")
                        
                        if is_hls_url and YTDLP_AVAILABLE:
                            # For HLS URLs, try yt-dlp directly (ensures fresh URLs)
                            try:
                                return await download_ytdlp(url, filename, progress_msg, start_time_ref, user_id, format_id=format_id, cancel_ref=cancel_ref)
                            except Exception:
                                pass
                        
                        # Otherwise, recurse with the direct URL
                        return await download_url(
                            link_api_url, filename, progress_msg, start_time_ref, user_id, 
                            format_id=format_id, cancel_ref=cancel_ref
                        )
                except Exception:
                    pass
                raise ValueError(str(ytdlp_err)) from ytdlp_err

    # Secondary extraction route: Force Cobalt for skipped yt-dlp domains (e.g. YouTube)
    if is_cobalt_url(url):
        try:
            return await download_cobalt(url, filename, progress_msg, start_time_ref, user_id, cancel_ref=cancel_ref)
        except Exception:
            pass # fall through to link-api / aria2c/http probe if cobalt fails

    # ── Fallback B: link-api for unknown URLs ─────────────────────────────────
    # For URLs that are not handled by yt-dlp or cobalt, try link-api to resolve
    # the actual stream URL (headless browser + yt-dlp server-side).
    # IMPORTANT: Only use extracted direct URLs for non-HLS streams.
    # For HLS streams, try yt-dlp directly instead.
    try:
        Config.LOGGER.info(f"Trying link-api/local extractor for unknown URL: {url[:80]}")
        link_api_url = await fetch_link_api(url)
        if link_api_url:
            Config.LOGGER.info(f"link-api resolved: {link_api_url[:80]}")
            
            # Check if the resolved URL is an HLS stream
            is_hls_url = ".m3u8" in link_api_url.lower() or link_api_url.lower().endswith("/m3u8")
            
            if is_hls_url:
                # For HLS URLs, try yt-dlp directly on the ORIGINAL URL instead
                # This ensures fresh URL extraction with proper auth tokens
                if YTDLP_AVAILABLE:
                    Config.LOGGER.info(f"HLS URL detected from extractor, trying yt-dlp directly on original URL: {url[:80]}")
                    try:
                        return await download_ytdlp(url, filename, progress_msg, start_time_ref, user_id, format_id=format_id, cancel_ref=cancel_ref)
                    except Exception:
                        pass
                
                # If yt-dlp still fails, try direct FFmpeg download with the HLS URL
                # But first, try to download with yt-dlp using the HLS URL
                if YTDLP_AVAILABLE:
                    try:
                        Config.LOGGER.info(f"Attempting direct yt-dlp download of HLS URL")
                        return await download_ytdlp(link_api_url, filename, progress_msg, start_time_ref, user_id, format_id=None, cancel_ref=cancel_ref)
                    except Exception:
                        pass
            
            # For non-HLS URLs, proceed as before
            await progress_msg.edit_text(
                "📥 **Resolving stream…**\n_(grabbed direct link, downloading…)_",
                reply_markup=cancel_button(user_id)
            )
            # Recurse with the direct URL so it goes through the normal probe path
            return await download_url(
                link_api_url, filename, progress_msg, start_time_ref, user_id,
                format_id=format_id, cancel_ref=cancel_ref
            )
    except asyncio.CancelledError:
        raise
    except Exception as link_e:
        Config.LOGGER.warning(f"link-api/local extractor fallback failed: {link_e}")

    # ── Probe the URL to detect content type ─────────────────────────────────
    session = await get_http_session()
    
    # For CDN URLs with HLS (like PornHub), try yt-dlp first to get fresh URLs
    if "phncdn.com" in url.lower() and ".m3u8" in url.lower():
        if YTDLP_AVAILABLE:
            Config.LOGGER.info(f"CDN HLS URL detected ({url[:80]}), trying yt-dlp for fresh extraction")
            try:
                return await download_ytdlp(url, filename, progress_msg, start_time_ref, user_id, format_id=format_id, cancel_ref=cancel_ref)
            except Exception as e:
                Config.LOGGER.warning(f"yt-dlp failed for CDN HLS URL: {e}")
                # Continue to FFmpeg fallback
    
    async with session.head(
        url, allow_redirects=True,
        timeout=aiohttp.ClientTimeout(total=30),
        proxy=Config.PROXY
    ) as head:
        mime = head.headers.get("Content-Type", "").split(";")[0].strip()
        total_str = head.headers.get("Content-Length", "0")
        total = int(total_str) if total_str.isdigit() else 0
        
        # Extract true filename if available from server
        cd = head.headers.get("Content-Disposition", "")
        cd_match = re.search(r'filename="?([^"]+)"?', cd)
        if cd_match:
            filename = cd_match.group(1)
        else:
            # If no Content-Disposition, and filename lacks an extension, guess via mime
            if not os.path.splitext(filename)[1]:
                ext = mimetypes.guess_extension(mime)
                if ext:
                    # Some systems return '.jpe' for jpeg
                    if ext == '.jpe': ext = '.jpg'
                    filename += ext

    # Re-evaluate safe filename based on true network name
    filename = smart_output_name(filename)
    safe_name = re.sub(r'[\\/*?:"<>|]', "_", filename)[:80]
    file_path = os.path.join(download_dir, safe_name)

    # ── Route HLS / DASH / TS streams through ffmpeg ──────────────────────────
    if needs_ffmpeg_download(url, mime):
        # Force mp4 output path
        mp4_path = os.path.splitext(file_path)[0] + ".mp4"
        try:
            await progress_msg.edit_text(
                "📥 **Downloading stream…**\n"
                "_(stitching stream chunks together…)_",
                reply_markup=cancel_button(user_id)
            )
        except Exception:
            pass
        await _download_hls(url, mp4_path, progress_msg, start_time_ref, user_id, cancel_ref=cancel_ref, headers=headers)
        return mp4_path, "video/mp4"

    # ── Aria2c High Speed Download ──────────────────────────────────────────────
    if total > Config.MAX_FILE_SIZE:
        raise ValueError(
            f"File too large: {humanbytes(total)} (max {humanbytes(Config.MAX_FILE_SIZE)})"
        )

    try:
        await _download_aria2c(url, file_path, progress_msg, start_time_ref, user_id, cancel_ref=cancel_ref)
    except RuntimeError as e:
        error_msg = str(e)
        # If aria2c fails with 403/401/407 (auth errors), try yt-dlp as fallback
        if "403" in error_msg or "401" in error_msg or "407" in error_msg:
            Config.LOGGER.warning(f"aria2c got auth error, trying yt-dlp: {error_msg}")
            if YTDLP_AVAILABLE:
                try:
                    return await download_ytdlp(url, filename, progress_msg, start_time_ref, user_id, format_id=None, cancel_ref=cancel_ref)
                except Exception:
                    pass
        raise

    mime_from_ext = mimetypes.guess_type(file_path)[0]
    final_mime = mime_from_ext or mime
    return file_path, final_mime



# ── Aria2c Custom Downloader ──────────────────────────────────────────────────

# Global aria2 client bound to the daemon we started in bot.py
aria2 = aria2p.API(
    aria2p.Client(
        host="http://localhost",
        port=6800,
        secret=""
    )
)

async def _download_aria2c(url: str, out_path: str, progress_msg, start_time_ref: list, user_id: int, cancel_ref: list = None, headers: dict = None) -> str:
    """
    Download extremely fast using aria2c native RPC daemon.
    Uses 16 concurrent HTTP streams and no file allocation for Koyeb disk stability.
    """
    start_time_ref[0] = time.time()
    last_edit = start_time_ref[0]
    
    # Ensure background aria2c daemon writes exactly where we expect
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    options = {
        "max-connection-per-server": "16",
        "split": "16",
        "min-split-size": "1M",
        "file-allocation": "none",
        "dir": os.path.dirname(out_path),
        "out": os.path.basename(out_path),
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "header": ["Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"]
    }
    
    # Inject global cookies directly into aria2c so it can authenticate social media direct links just like yt-dlp
    if os.path.exists(Config.COOKIES_FILE):
        options["load-cookies"] = os.path.abspath(Config.COOKIES_FILE)

    # Merge custom headers (e.g. Cookies, Referer from yt-dlp)
    if headers:
        if "header" not in options:
            options["header"] = []
        for k, v in headers.items():
            # aria2 expects "Name: Value" strings in the header list
            options["header"].append(f"{k}: {v}")
        if "User-Agent" in headers:
            options["user-agent"] = headers["User-Agent"]

    # Add the download to the daemon
    download = await asyncio.to_thread(aria2.add_uris, [url], options)

    try:
        while True:
            await asyncio.to_thread(download.update)

            if download.is_complete:
                break

            if cancel_ref and cancel_ref[0]:
                await asyncio.to_thread(download.remove, force=True, files=True)
                raise asyncio.CancelledError("Download cancelled.")
            
            if download.has_failed:
                error_msg = download.error_message
                await asyncio.to_thread(download.remove, force=True, files=True)
                raise RuntimeError(f"aria2c download failed: {error_msg}")

            pct_int = int(download.progress)
            _bar = progress_bar(pct_int, 100)
            
            speed_str = download.download_speed_string()
            current_str = download.completed_length_string()
            total_str = download.total_length_string()

            # Cap at 99.5% to avoid jumping to "Complete" before merging/uploading
            display_pct = pct_int
            if display_pct >= 100:
                display_pct = 99.5

            # Update Telegram message (every 1s) to avoid flood
            now = time.time()
            if now - last_edit >= PROGRESS_UPDATE_DELAY:
                text = (
                    f"📥 **Downloading Media…** ⬇️\n\n"
                    f"📁 **Name:** `{os.path.basename(out_path)}`\n"
                    f"[{_bar}] {download.progress_string()}\n"
                    f"**Done:** {current_str} / {total_str}\n"
                    f"**Speed:** {speed_str}\n"
                    f"**ETA:** {download.eta_string()}"
                )
                try:
                    await progress_msg.edit_text(text, reply_markup=cancel_button(user_id))
                except Exception:
                    pass
                last_edit = now

            await asyncio.sleep(0.2) # High frequency polling for smooth UI

        return out_path
    except Exception:
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise


# ── Watermark helper ──────────────────────────────────────────────────────────

# (apply_watermark is defined above near the top of this file)


# ── Upload helper ─────────────────────────────────────────────────────────────

async def upload_file(
    client: Client,
    chat_id: int,
    file_path: str,
    mime: str,
    caption: str,
    thumb_file_id: str | None,
    progress_msg,
    start_time_ref: list,
    user_id: int,
    force_document: bool = False,
    cancel_ref: list = None,
    watermark: dict | None = None,
):
    """
    Upload a local file to Telegram with:
    - Live progress bar
    - Correct duration / width / height for videos (extracted via ffprobe)
    - Auto-generated thumbnail from the video frame if no custom thumb is set
    - Custom thumbnail (downloaded from Telegram by file_id) if set by user
    - Watermark overlay on thumbnail (premium users only)
    """

    last_edit = [time.time()]
    start_time_ref[0] = time.time()

    async def _progress(current: int, total: int):
        if cancel_ref and cancel_ref[0]:
            raise asyncio.CancelledError("Upload cancelled.")
            
        now = time.time()
        done = current
        percent = (done / total * 100) if total else 0
        elapsed = now - start_time_ref[0]
        speed = done / elapsed if elapsed else 0
        eta = (total - done) / speed if speed else 0
        
        # Update Telegram message
        if now - last_edit[0] < PROGRESS_UPDATE_DELAY:
            return
            
        bar = progress_bar(done, total)
        text = (
            "📤 **Uploading…**\n\n"
            f"📁 **Name:** `{os.path.basename(file_path)}`\n"
            f"{bar}\n"
            f"**Done:** {humanbytes(done)} / {humanbytes(total)}\n"
            f"**Speed:** {humanbytes(speed)}/s\n"
            f"**ETA:** {time_formatter(eta)}"
        )
        try:
            await progress_msg.edit_text(text, reply_markup=cancel_button(chat_id))
        except Exception:
            pass
        last_edit[0] = now

    os.makedirs(Config.DOWNLOAD_LOCATION, exist_ok=True)
    is_video = not force_document and bool(mime and mime.startswith("video/"))
    is_audio = not force_document and bool(mime and mime.startswith("audio/"))
    is_image = not force_document and bool(mime and mime.startswith("image/"))

    # ── 1. Get video metadata (duration, width, height) ───────────────────────
    meta = {"duration": 0, "width": 0, "height": 0}
    if is_video:
        try:
            await progress_msg.edit_text("🔍 Reading video metadata…", reply_markup=cancel_button(chat_id))
        except Exception:
            pass
        meta = await get_video_metadata(file_path)

    # Truncate caption to Telegram limit (1024)
    if caption and len(caption) > 1000:
        caption = caption[:997] + "..."

    # ── 2. Resolve thumbnail ───────────────────────────────────────────────────
    # Use a unique thumb name to avoid conflicts during concurrent uploads
    thumb_suffix = abs(hash(file_path)) % 10000
    thumb_local = None

    if thumb_file_id:
        try:
            # First, download to a generic path to see what extension Telegram gives us
            temp_thumb = await client.download_media(
                thumb_file_id,
                file_name=os.path.join(Config.DOWNLOAD_LOCATION, f"raw_thumb_{chat_id}_{thumb_suffix}"),
            )
            if temp_thumb and os.path.exists(temp_thumb):
                # Now normalize it with Pillow: Resize to 320x320 (proportional) and convert to JPEG
                normalized_path = os.path.join(Config.DOWNLOAD_LOCATION, f"thumb_user_{chat_id}_{thumb_suffix}.jpg")
                try:
                    with Image.open(temp_thumb) as img:
                        img.thumbnail((320, 320))
                        if img.mode != 'RGB':
                            img = img.convert('RGB')
                        img.save(normalized_path, "JPEG", quality=85, optimize=True)
                    thumb_local = normalized_path
                except Exception as img_err:
                    Config.LOGGER.error(f"Thumbnail normalization failed for {chat_id}: {img_err}")
                finally:
                    # Cleanup the raw download
                    if os.path.exists(temp_thumb):
                        os.remove(temp_thumb)
        except Exception as dl_err:
            Config.LOGGER.error(f"Custom thumbnail download failed for {chat_id}: {dl_err}")
            thumb_local = None

    if not thumb_local and is_video:
        try:
            await progress_msg.edit_text("🖼️ Generating fast thumbnail…", reply_markup=cancel_button(chat_id))
        except Exception:
            pass
        # Ensure duration is at least 1s for better thumbnail compatibility
        v_duration = max(1, meta["duration"])
        thumb_local = await generate_video_thumbnail(file_path, f"{chat_id}_{thumb_suffix}", v_duration)

    # ── 2.5 Apply Watermark ───────────────────────────────────────────────────
    if thumb_local and watermark and (watermark.get("text") or watermark.get("image")):
        wm_image_path = None
        if watermark.get("image"):
            try:
                wm_image_path = await client.download_media(
                    watermark["image"],
                    file_name=os.path.join(Config.DOWNLOAD_LOCATION, f"wm_img_{chat_id}_{thumb_suffix}")
                )
            except Exception as e:
                Config.LOGGER.error(f"Watermark image download failed: {e}")
                
        try:
            with Image.open(thumb_local) as img:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img = apply_watermark(img, watermark, wm_image_path)
                img.save(thumb_local, "JPEG", quality=85, optimize=True)
        except Exception as e:
            Config.LOGGER.error(f"Applying watermark failed: {e}")
        finally:
            if wm_image_path and os.path.exists(wm_image_path):
                try:
                    os.remove(wm_image_path)
                except Exception:
                    pass

    # ── 3. Build kwargs (chat_id and file passed as positional args) ───────────
    kwargs = dict(
        caption=caption,
        parse_mode=None,
        progress=_progress,
    )
    if thumb_local:
        kwargs["thumb"] = thumb_local

    # ── 4. Send to Telegram ───────────────────────────────────────────────────
    try:
        # Final safety checks for metadata types
        v_duration = int(meta.get("duration", 0))
        v_width = int(meta.get("width", 0))
        v_height = int(meta.get("height", 0))

        if force_document:
            await client.send_document(chat_id, file_path, **kwargs)
        elif is_video:
            await client.send_video(
                chat_id,
                file_path,
                duration=v_duration,
                width=v_width,
                height=v_height,
                supports_streaming=True,
                **kwargs,
            )
        elif is_audio:
            await client.send_audio(chat_id, file_path, **kwargs)
        elif is_image:
            # 🚨 FIX: Pyrogram Photo messages DO NOT support thumbnails. 
            # If thumb_local is set, we MUST NOT pass it to send_photo.
            await client.send_photo(chat_id, file_path, caption=caption, progress=_progress)
        else:
            await client.send_document(chat_id, file_path, **kwargs)
    except Exception as e:
        Config.LOGGER.error(f"Critical Pyrogram send error for {file_path}: {e}")
        # Log more info to help debug serialization issues
        Config.LOGGER.error(f"Metadata: {meta}, Thumb: {thumb_local}, Caption Len: {len(caption) if caption else 0}, Mime: {mime}")
        raise
    finally:
        # Clean up any temp thumbnail files
        if thumb_local and os.path.exists(thumb_local):
            try:
                os.remove(thumb_local)
            except Exception:
                pass
