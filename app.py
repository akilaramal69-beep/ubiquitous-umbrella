import os
import asyncio
import urllib.parse
from flask import Flask, request, jsonify
from plugins.config import Config
import time

app = Flask(__name__)

app.is_ready = False
app.is_shutting_down = False

async def prune_progress_task():
    """Background task to keep memory low."""
    while True:
        try:
            await asyncio.sleep(600)
        except Exception:
            pass

@app.route("/")
def index():
    if app.is_shutting_down:
        return {"status": "shutting_down"}, 503
    if not app.is_ready:
        return {"status": "starting"}, 503
    return {"status": "ok", "service": "URL Uploader Bot API"}, 200

@app.route("/health")
def health():
    if app.is_shutting_down:
        return {"status": "shutting_down"}, 503
    if not app.is_ready:
        return {"status": "starting"}, 503
    return {"status": "ok"}, 200


@app.route("/grab", methods=["GET"])
def grab_get():
    """Extract direct media links from any video URL (GET)."""
    if not app.is_ready:
        return {"error": "Bot is not ready"}, 503

    url = request.args.get("url")
    if not url:
        return {"error": "No URL provided"}, 400

    if not _is_valid_url(url):
        return {"error": "Invalid URL"}, 400

    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        return {"error": "YouTube not supported"}, 400

    try:
        from plugins.helper.extractor import extract_links
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(extract_links(url, use_browser=True, timeout=45))
        loop.close()
        if not result.get("links"):
            return {"error": f"No media links found for: {url}"}, 400
        return result, 200
    except Exception as e:
        return {"error": f"Extraction error: {str(e)}"}, 400


@app.route("/grab", methods=["POST"])
def grab_post():
    """Extract direct media links from any video URL (POST)."""
    if not app.is_ready:
        return {"error": "Bot is not ready"}, 503

    data = request.json or {}
    url = data.get("url")
    if not url:
        return {"error": "No URL provided"}, 400

    if not _is_valid_url(url):
        return {"error": "Invalid URL"}, 400

    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        return {"error": "YouTube not supported"}, 400

    try:
        from plugins.helper.extractor import extract_links
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(extract_links(url, use_browser=True, timeout=45))
        loop.close()
        if not result.get("links"):
            return {"error": f"No media links found for: {url}"}, 400
        return result, 200
    except Exception as e:
        return {"error": f"Extraction error: {str(e)}"}, 400


@app.route("/extract", methods=["POST"])
def extract_post():
    """Raw yt-dlp extraction for drop-in compatibility."""
    if not app.is_ready:
        return {"error": "Bot is not ready"}, 503

    data = request.json or {}
    url = data.get("url")
    if not url:
        return {"error": "Missing 'url' in JSON body"}, 400

    if not _is_valid_url(url):
        return {"error": "Invalid URL"}, 400

    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        return {"error": "YouTube not supported"}, 400

    try:
        from plugins.helper.extractor import extract_raw_ytdlp
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(extract_raw_ytdlp(url))
        loop.close()
        return result, 200
    except Exception as e:
        return {"error": str(e), "formats": [], "title": "Extraction Failed"}, 200


def _is_valid_url(url: str) -> bool:
    """Basic URL validation."""
    try:
        parsed = urllib.parse.urlparse(url)
        return bool(parsed.scheme in ('http', 'https') and parsed.netloc)
    except Exception:
        return False


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
