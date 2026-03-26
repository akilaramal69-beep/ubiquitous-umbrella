import asyncio
import re
import httpx
from typing import Optional
from plugins.config import Config

try:
    from plugins.helper.browser_extractor import intercept_browser, MEDIA_URL_PATTERNS
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    MEDIA_URL_PATTERNS = re.compile(r"(\.(mp4|m3u8|m4v|m4a|mpd|ts|webm|mkv|flv|avi|mov|aac|mp3|ogg|opus)([?&]|$))|remote_control\.php", re.IGNORECASE)

SEGMENT_PATTERNS = re.compile(
    r"[-_](seg|chunk|part|frag|fragment|track|init|video\d|audio\d)[-_]|\d+\.ts|\d+\.m4v",
    re.IGNORECASE,
)


async def extract_links(url: str, use_browser: bool = True, timeout: int = 25) -> dict:
    browser_results = []
    errors = []

    is_direct = bool(MEDIA_URL_PATTERNS.search(url.split('?')[0]))

    if use_browser and PLAYWRIGHT_AVAILABLE:
        try:
            if not is_direct:
                browser_results = await intercept_browser(url, timeout_ms=timeout * 1000)
            else:
                browser_results = [{
                    "url": url,
                    "stream_type": _guess_type_from_url(url),
                    "source": "direct_input"
                }]
        except Exception as e:
            errors.append(f"browser_error: {e}")
    elif use_browser and not PLAYWRIGHT_AVAILABLE:
        errors.append("Playwright not available - falling back to yt-dlp")

    if not browser_results:
        # Fallback: try yt-dlp extraction
        try:
            import yt_dlp
            ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and info.get('url'):
                    browser_results = [{
                        "url": info.get('url'),
                        "stream_type": "ytdlp",
                        "source": "ytdlp_fallback",
                        "title": info.get('title'),
                        "thumbnail": info.get('thumbnail'),
                        "duration": info.get('duration')
                    }]
        except Exception as e:
            errors.append(f"ytdlp_error: {e}")

    if not browser_results:
        raise RuntimeError(
            f"Could not extract any links. Details — {'; '.join(errors)}"
        )

    response: dict = {
        "url": url,
        "title": "Extracted Video",
        "thumbnail": None,
        "duration": None,
        "extractor": "BrowserIntercept",
        "uploader": None,
    }

    filtered_browser_links = []
    AD_KEYWORDS = ("ads", "vast", "click", "pop", "preroll", "midroll", "postroll", "sponsored")
    
    for link in browser_results:
        if any(k in link["url"].lower() for k in AD_KEYWORDS):
            continue

        is_segment = bool(SEGMENT_PATTERNS.search(link["url"]))
        if is_segment and link.get("stream_type") != "hls":
            continue

        if link.get("stream_type") == "hls":
            filtered_browser_links.append(link)
            continue
            
        length = link.get("content_length")
        if length and length < 1_500_000:
            continue
            
        u_lower = link["url"].lower()
        if any(ext in u_lower for ext in (".html", ".htm", ".php", ".jsp", ".aspx")):
             if "remote_control.php" not in u_lower and "get_file" not in u_lower:
                 continue
                 
        filtered_browser_links.append(link)

    all_links = list(filtered_browser_links)

    seen = set()
    unique_links = []
    for link in all_links:
        u = link["url"]
        if u not in seen:
            seen.add(u)
            unique_links.append(link)

    async def _validate_link(link_item: dict) -> Optional[dict]:
        url = link_item["url"]
        
        u_lower = url.lower().split('?')[0]
        if any(u_lower.endswith(ext) for ext in (".m3u8", ".mp4", ".mpd", ".webm")):
             return link_item
             
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                headers = {"User-Agent": "Mozilla/5.0", "Referer": link_item.get("referer") or url}
                resp = await client.head(url, headers=headers)
                
                if resp.status_code >= 400:
                    headers["Range"] = "bytes=0-0"
                    resp = await client.get(url, headers=headers)
                
                ct = resp.headers.get("content-type", "").lower()
                
                if "text/html" in ct or "application/json" in ct or "text/plain" in ct:
                    return None
                    
                if "video" in ct or "audio" in ct or "mpegurl" in ct or "dash+xml" in ct:
                    link_item["content_type"] = ct
                    if not link_item.get("content_length"):
                        link_item["content_length"] = int(resp.headers.get("content-length", "0") or "0")
                    return link_item
                
                if "octet-stream" in ct:
                     return link_item
                     
        except Exception:
            if any(ext in u_lower for ext in (".php", ".html", ".aspx", ".jsp")):
                return None
            return link_item
            
        return None

    validated_tasks = [asyncio.create_task(_validate_link(l)) for l in unique_links]
    validated_results = await asyncio.gather(*validated_tasks)
    final_links = [l for l in validated_results if l is not None]

    final_links.sort(
        key=lambda x: (
            bool(x.get("has_video") and x.get("has_audio")),
            x.get("height") or 0,
            x.get("filesize") or x.get("content_length") or 0,
        ),
        reverse=True,
    )

    response["links"] = final_links
    response["total"] = len(final_links)
    response["best_link"] = _pick_best(final_links)
    response["errors"] = errors if errors else None

    return response




def _pick_best(links: list) -> Optional[str]:
    if not links:
        return None
        
    AD_KEYWORDS = (
        "ads", "vast", "crossdomain", "traffic", "click", "pop", "pre-roll", 
        "mid-roll", "post-roll", "creative", "affiliate", "tracking", "pixel"
    )
    AD_DOMAINS = ("contentabc.com", "exoclick.com", "doubleclick.net", "googlesyndication.com")
    
    clean_links = []
    for l in links:
        url_lower = l["url"].lower()
        referer_lower = (l.get("referer") or "").lower()
        
        if any(k in url_lower or k in referer_lower for k in AD_KEYWORDS):
            continue
        if any(d in url_lower or d in referer_lower for d in AD_DOMAINS):
            continue
            
        clean_links.append(l)
    
    target_links = clean_links if clean_links else links

    for link in target_links:
        u = link["url"].lower()
        if "remote_control.php" in u or "get_file" in u:
            return link["url"]

    for link in target_links:
        if link.get("source", "").startswith("js_") and ".m3u8" in link["url"]:
             return link["url"]

    MASTER_MANIFEST_KEYWORDS = ("master", "playlist", "index", "manifest", "m3u8", "main")
    for link in target_links:
        if link.get("stream_type") == "hls":
            u = link["url"].lower()
            if any(k in u for k in MASTER_MANIFEST_KEYWORDS):
                if not SEGMENT_PATTERNS.search(u):
                    return link["url"]
            
    for link in target_links:
        if link.get("stream_type") == "hls":
            return link["url"]
            
    for link in target_links:
        if link.get("source", "").startswith("js_") and link.get("stream_type") in ("mp4", "webm"):
             return link["url"]

    for link in target_links:
        if link.get("has_video") and link.get("has_audio"):
            return link["url"]
            
    for link in target_links:
        if link.get("stream_type") == "mp4":
            return link["url"]
            
    for link in target_links:
        u = link["url"].lower()
        if not any(k in u for k in (".php", ".html", ".htm", ".jsp", ".aspx")):
            return link["url"]
            
    return None


def _guess_type_from_url(url: str) -> str:
    path = url.split('?')[0].lower()
    if ".m3u8" in path: return "hls"
    if ".mpd" in path: return "dash"
    if ".mp4" in path: return "mp4"
    if ".webm" in path: return "webm"
    return "unknown"


async def extract_raw_ytdlp(url: str) -> dict:
    """
    Run browser interception and return results in the legacy yt-dlp format
    for drop-in compatibility with older bots.
    """
    try:
        res = await extract_links(url, use_browser=True, timeout=25)
        
        fake_info = {
            "id": "browser_extract",
            "title": res.get("title", "Extracted Video"),
            "thumbnail": res.get("thumbnail"),
            "duration": res.get("duration"),
            "extractor": "Playwright",
            "webpage_url": url,
            "formats": []
        }

        for i, link in enumerate(res.get("links", [])):
            has_video = link.get("has_video", True)
            has_audio = link.get("has_audio", True)
            height = link.get("height") or 720
            
            fmt = {
                "format_id": f"browser_{i}",
                "url": link["url"],
                "ext": (link.get("content_type") or "video/mp4").split("/")[-1] or "mp4",
                "width": link.get("width", 1920),
                "height": height,
                "resolution": f"{height}p",
                "vcodec": "avc1" if has_video else "none",
                "acodec": "mp4a" if has_audio else "none",
                "filesize": link.get("filesize") or link.get("content_length"),
                "has_audio": has_audio,
                "bitrate": 0,
                "source": link.get("source")
            }
            
            if link.get("stream_type") == "hls":
                fmt["protocol"] = "m3u8_native"
                fmt["ext"] = "mp4"
                fmt["format_note"] = "HLS Stream"
                fmt["vcodec"] = "avc1"
                fmt["acodec"] = "mp4a"
            
            fake_info["formats"].append(fmt)

        return fake_info

    except Exception as e:
        raise ValueError(f"Unified extraction failed: {e}")
