import asyncio
import re
from urllib.parse import urlparse
from playwright.async_api import async_playwright, Request

MEDIA_CONTENT_TYPES = (
    "video/",
    "audio/",
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "application/dash+xml",
    "application/octet-stream",
)

MEDIA_URL_PATTERNS = re.compile(
    r"(\.(mp4|m3u8|m4v|m4a|mpd|ts|webm|mkv|flv|avi|mov|aac|mp3|ogg|opus)([?&]|$))|remote_control\.php",
    re.IGNORECASE,
)

IGNORE_PATTERNS = re.compile(
    r"(doubleclick|googlesyndication|adservice|analytics|googletagmanager"
    r"|exoclick|trafficjunky|chaturbate|jerkmate|bongacams|stripchat|popads"
    r"|bidgear|adsco|outbrain|taboola|mgid|vast|vpaid|ima3|preroll|midroll"
    r"|postroll|advertisement|branded|sponsor|tracking|pixel|beacon"
    r"|popunder|clickunder|onclick|adsterra|propellerads|adespresso|yandex"
    r"|\.jpg|\.jpeg|\.png|\.gif|\.webp|\.svg|\.ico|\.css|\.js|\.woff|\.ttf"
    r"|\.php|\.html|\.htm|\.jsp|\.aspx|/ads/|/ad/|/pixel)",
    re.IGNORECASE,
)
MIN_CONTENT_LENGTH = 50_000


def _guess_height_from_url(url: str) -> int | None:
    """Guess video height from URL patterns."""
    url_lower = url.lower()
    if "1080" in url_lower or "fhd" in url_lower or "fullhd" in url_lower:
        return 1080
    elif "720" in url_lower or "/hd/" in url_lower:
        return 720
    elif "480" in url_lower:
        return 480
    elif "360" in url_lower:
        return 360
    elif "240" in url_lower:
        return 240
    return None


SNIFFER_JS = r"""
(function() {
    const seenUrls = new Set();
    const logMedia = (url, source) => {
        if (!url || typeof url !== 'string' || url.startsWith('blob:') || url.startsWith('data:')) return;
        if (seenUrls.has(url)) return;
        seenUrls.add(url);
        if (window.pythonSniff) {
            window.pythonSniff(url, source);
        }
    };

    const originalSrcDescriptor = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
    Object.defineProperty(HTMLMediaElement.prototype, 'src', {
        set: function(val) {
            logMedia(val, 'media_proto_src');
            return originalSrcDescriptor.set.apply(this, arguments);
        },
        get: function() {
            return originalSrcDescriptor.get.apply(this, arguments);
        }
    });

    const monitorElement = (el) => {
        if (el._sniffed) return;
        el._sniffed = true;
        ['loadstart', 'play', 'playing', 'loadedmetadata', 'canplay'].forEach(ev => {
            el.addEventListener(ev, () => {
                logMedia(el.src || el.currentSrc, 'media_event_' + ev);
            }, { passive: true });
        });
        
        if (el.player) {
             const p = el.player;
             if (p.currentSrc && typeof p.currentSrc === 'function') logMedia(p.currentSrc(), 'videojs_src');
        }
    };

    document.querySelectorAll('video, audio').forEach(monitorElement);

    const observer = new MutationObserver((mutations) => {
        mutations.forEach(m => {
            m.addedNodes.forEach(node => {
                if (node.nodeName === 'VIDEO' || node.nodeName === 'AUDIO') monitorElement(node);
                if (node.querySelectorAll) node.querySelectorAll('video, audio').forEach(monitorElement);
            });
        });
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });

    setInterval(() => {
        document.querySelectorAll('video, audio').forEach(el => {
            logMedia(el.currentSrc || el.src, 'media_poll');
        });
    }, 1000);

    const originalOpen = window.open;
    window.open = function(url, name, specs) {
        logMedia(url, 'window_open');
        return originalOpen.apply(this, arguments);
    };

    const originalFetch = window.fetch;
    window.fetch = async function(...args) {
        const response = originalFetch.apply(this, args);
        const clone = response.clone();
        try {
            const data = await clone.text();
            if (data.includes('.m3u8') || data.includes('.mp4')) {
                const matches = data.match(/https?:[^\s"'<>]+\.(?:m3u8|mp4|mpd)(?:\?[^\s"'<>]*)?/g);
                if (matches) matches.forEach(m => logMedia(m, 'fetch_intercept'));
            }
        } catch(e) {}
        return response;
    };

    const scanDomForHiddenUrls = () => {
        document.querySelectorAll('script').forEach(s => {
            const content = s.textContent || s.innerText;
            if (content && (content.includes('.m3u8') || content.includes('.mp4'))) {
                const matches = content.match(/https?:[^\s"'<>]+\.(?:m3u8|mp4|mpd)(?:\?[^\s"'<>]*)?/g);
                if (matches) matches.forEach(m => logMedia(m, 'script_scan'));
            }
        });

        const allElements = document.getElementsByTagName("*");
        for (let i = 0; i < allElements.length; i++) {
            const el = allElements[i];
            for (let j = 0; j < el.attributes.length; j++) {
                const attr = el.attributes[j];
                if (attr.name.startsWith('data-') || attr.name === 'value' || attr.name === 'src') {
                    const val = attr.value;
                    if (val && (val.includes('.m3u8') || val.includes('.mp4'))) {
                         const matches = val.match(/https?:[^\s"'<>]+\.(?:m3u8|mp4|mpd)(?:\?[^\s"'<>]*)?/g);
                         if (matches) matches.forEach(m => logMedia(m, 'attr_regex_scan'));
                    }
                }
            }
        }
    };
    
    scanDomForHiddenUrls();
    setInterval(scanDomForHiddenUrls, 3000);
})();
"""

async def intercept_browser(url: str, timeout_ms: int = 25000) -> list[dict]:
    found: dict[str, dict] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--mute-audio",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        await context.add_init_script(SNIFFER_JS)

        page = await context.new_page()

        async def block_resources(route):
            if route.request.resource_type in ["image", "media", "font"]:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", block_resources)

        page.on("popup", lambda p: asyncio.create_task(p.close()))
        page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))

        async def python_sniff(source_info, media_url, sniffer_source):
            if media_url and media_url.startswith('http'):
                if not IGNORE_PATTERNS.search(media_url) or MEDIA_URL_PATTERNS.search(media_url):
                    _add_media_entry(found, media_url, source=f"js_{sniffer_source}")

        await page.expose_binding("pythonSniff", python_sniff)

        async def on_request(request: Request):
            req_url = request.url
            if IGNORE_PATTERNS.search(req_url) and not MEDIA_URL_PATTERNS.search(req_url):
                return
            if MEDIA_URL_PATTERNS.search(req_url):
                _add_media_entry(found, req_url, source="url_pattern", request=request)

        async def on_response(response):
            try:
                resp_url = response.url
                is_media_url = bool(MEDIA_URL_PATTERNS.search(resp_url))
                
                if not is_media_url and IGNORE_PATTERNS.search(resp_url):
                    return

                content_type = response.headers.get("content-type", "").lower()
                content_length = int(response.headers.get("content-length", "0") or "0")
                
                if "text/" in content_type or "html" in content_type or "json" in content_type:
                    return

                if "html" in content_type:
                    return

                is_media_type = any(mt in content_type for mt in MEDIA_CONTENT_TYPES)

                # CDN quality detection - check for high-quality patterns
                quality_indicators = {
                    "has_video": "video" in content_type or ".mp4" in resp_url.lower() or ".m4v" in resp_url.lower(),
                    "has_audio": "audio" in content_type or ".mp3" in resp_url.lower() or ".m4a" in resp_url.lower(),
                    "is_hls": ".m3u8" in resp_url.lower(),
                    "is_dash": ".mpd" in resp_url.lower(),
                    "height": _guess_height_from_url(resp_url),
                }

                if is_media_type or is_media_url:
                    # Only filter very small files (<50KB) unless it's a direct media URL
                    if content_length > 0 and content_length < MIN_CONTENT_LENGTH and not is_media_url:
                        return
                    _add_media_entry(
                        found,
                        resp_url,
                        source="response_header",
                        content_type=content_type,
                        content_length=content_length,
                        has_video=quality_indicators.get("has_video"),
                        has_audio=quality_indicators.get("has_audio"),
                        height=quality_indicators.get("height"),
                        is_hls=quality_indicators.get("is_hls"),
                        is_dash=quality_indicators.get("is_dash"),
                    )
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            for attempt in range(2):
                try:
                    await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    break
                except Exception as e:
                    if attempt == 1: raise e
                    await asyncio.sleep(2)

            await asyncio.sleep(12.0)

            for _ in range(3):
                frames = page.frames
                for frame in frames:
                    try:
                        dom_links = await frame.evaluate(r"""() => {
                            const urls = new Set();
                            document.querySelectorAll('video, audio, source').forEach(el => {
                                if (el.src) urls.add(el.src);
                                if (el.currentSrc) urls.add(el.currentSrc);
                            });
                            const pageText = document.documentElement.innerHTML;
                            const matches = pageText.match(/https?:[^\s"'<>]+\.(?:m3u8|mp4|mpd)(?:\?[^\s"'<>]*)?/g);
                            if (matches) matches.forEach(m => urls.add(m));

                            return Array.from(urls).filter(u => u.startsWith('http'));
                        }""")
                        for link in dom_links:
                            if not IGNORE_PATTERNS.search(link) or MEDIA_URL_PATTERNS.search(link):
                                _add_media_entry(found, link, source="dom_scan")
                    except Exception:
                        pass

                    selectors = [
                        "button[aria-label*='play' i]", "button[class*='play' i]",
                        "button:has-text('Download')", "a:has-text('Download')",
                        "div.download", ".download-button", ".play-button",
                        "video", "[data-testid*='play' i]", ".vjs-big-play-button"
                    ]
                    
                    for selector in selectors:
                        try:
                            elements = await frame.query_selector_all(selector)
                            for el in elements:
                                if await el.is_visible():
                                    try:
                                        await el.click(timeout=1500, no_wait_after=True)
                                        await asyncio.sleep(0.5)
                                    except: pass
                                    
                                    sub_selectors = [
                                        "li:has-text('p')", "a:has-text('p')",
                                        "li:has-text('MP4')", "a:has-text('MP4')",
                                        "li:has-text('Download')", "a:has-text('Download')"
                                    ]
                                    for sub in sub_selectors:
                                        try:
                                            sub_el = await frame.query_selector(sub)
                                            if sub_el and await sub_el.is_visible():
                                                await sub_el.click(timeout=1000, no_wait_after=True)
                                                await asyncio.sleep(0.5)
                                        except Exception:
                                            pass
                        except Exception:
                            pass
                await asyncio.sleep(3)

        except Exception as e:
            if not found:
                raise RuntimeError(f"Browser navigation failed: {e}")
        finally:
            await browser.close()

    return list(found.values())


def _guess_height_from_url(url: str) -> int | None:
    """Guess video height from URL patterns."""
    url_lower = url.lower()
    if "1080" in url_lower or "fhd" in url_lower or "fullhd" in url_lower:
        return 1080
    elif "720" in url_lower or "hd" in url_lower:
        return 720
    elif "480" in url_lower:
        return 480
    elif "360" in url_lower:
        return 360
    elif "240" in url_lower:
        return 240
    return None


def _add_media_entry(
    found: dict,
    url: str,
    source: str = "unknown",
    request=None,
    content_type: str = "",
    content_length: int = 0,
    has_video: bool = True,
    has_audio: bool = True,
    height: int | None = None,
    is_hls: bool = False,
    is_dash: bool = False,
):
    if url in found:
        return

    parsed = urlparse(url)
    path = parsed.path.lower()

    if ".m3u8" in path or is_hls:
        stream_type = "hls"
    elif ".mpd" in path or is_dash:
        stream_type = "dash"
    elif ".mp4" in path or ".m4v" in path:
        stream_type = "mp4"
    elif ".webm" in path:
        stream_type = "webm"
    elif ".mp3" in path or ".aac" in path or ".m4a" in path or ".ogg" in path or ".opus" in path:
        stream_type = "audio"
    elif ".ts" in path:
        stream_type = "ts_segment"
    elif "video" in content_type:
        stream_type = "video"
    elif "audio" in content_type:
        stream_type = "audio"
    else:
        stream_type = "unknown"

    # Guess height from URL if not provided
    if height is None:
        height = _guess_height_from_url(url)

    found[url] = {
        "url": url,
        "stream_type": stream_type,
        "content_type": content_type or None,
        "content_length": content_length or None,
        "source": source,
        "referer": request.headers.get("referer") if request else None,
        "has_video": has_video,
        "has_audio": has_audio,
        "height": height,
    }
