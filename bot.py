import os
import subprocess
import sys
import threading
import asyncio
import time
from plugins.config import Config
import platform
import zipfile
import urllib.request
import atexit
from pyrogram import Client, idle, filters
import app  # noqa: F401

from utils.shared import bot_client, WEBAPP_PROGRESS

# Register a global ping handler for diagnostics
@bot_client.on_message(filters.command("ping") & filters.private)
async def ping_handler(client, message):
    print(f"📥 Received /ping from {message.from_user.id} at {time.time()}")
    await message.reply_text("🏓 Pong! Bot is alive and well.")

def run_health_server():
    from app import app as flask_app
    from waitress import serve
    print("🌍 Starting health & progress server with Waitress (Production)...")
    serve(flask_app, host="0.0.0.0", port=8080, threads=100)


def setup_po_token_server():
    """
    Ensure the Node.js PO Token server dependencies are installed dynamically
    so we don't need to manually check-in node_modules to GitHub.
    """
    import shutil
    if not shutil.which("npm"):
        print("⚠️ Warning: 'npm' not found. Skipping Node.js PO Token server setup.")
        return None

    if not os.path.exists("package.json"):
        print("📦 Initializing package.json for PO Token server...")
        subprocess.run(["npm", "init", "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
    if not os.path.exists("node_modules/youtube-po-token-generator") or not os.path.exists("node_modules/express"):
        print("📦 Installing Express and YouTube PO Token Generator dependencies...")
        subprocess.run(
            ["npm", "install", "express", "youtube-po-token-generator"],
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )
        print("✅ Node.js dependencies installed.")
    
    return "po_server.js"


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🚀  URL Uploader Bot — Starting…")
    print("=" * 60 + "\n")

    # ── Validate Playwright installation ───────────────────────────────────────
    try:
        from playwright.sync_api import sync_playwright
        print("✅ Playwright is installed.")
    except ImportError:
        print("⚠️ Playwright not found. Installing...")
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            subprocess.run([sys.executable, "-m", "playwright", "install-deps"], check=True)
            print("✅ Playwright installed successfully.")
        except Exception as e:
            print(f"⚠️ Could not install Playwright: {e}")
            print("   Browser-based extraction will not work.")

    # ── Validate required environment variables ──────────────────────────
    missing = []
    if not Config.BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not Config.API_ID:
        missing.append("API_ID")
    if not Config.API_HASH:
        missing.append("API_HASH")
    if missing:
        print(f"❌ FATAL: Missing required environment variables: {', '.join(missing)}")
        print("   Set them in .env or in your Koyeb environment settings.")
        sys.exit(1)

    # Ensure download folder exists and is clean on startup
    if os.path.exists(Config.DOWNLOAD_LOCATION):
        import shutil
        try:
            shutil.rmtree(Config.DOWNLOAD_LOCATION)
            print("🧹 Cleaned old DOWNLOADS folder on startup.")
        except Exception as e:
            print(f"⚠️ Could not clean DOWNLOADS folder: {e}")
    os.makedirs(Config.DOWNLOAD_LOCATION, exist_ok=True)

    # Handle cookies from environment variable (useful for Koyeb)
    # Koyeb env vars may store newlines as literal \n — convert them
    cookies_data = os.environ.get("COOKIES_DATA", "")
    if cookies_data:
        cookies_data = cookies_data.replace("\\n", "\n")
        try:
            with open(Config.COOKIES_FILE, "w", encoding="utf-8") as f:
                f.write(cookies_data)
            print(f"🍪 Cookies written to {Config.COOKIES_FILE} from COOKIES_DATA env var.")
        except Exception as e:
            print(f"❌ Failed to write cookies file: {e}")

    # ── Start Background Services ──────────────────────────────────────────
    
    print("🚀 Starting youtube-po-token-generator (Node.js) server...")
    po_script = setup_po_token_server()
    pot_process = None
    if po_script and os.path.exists(po_script):
        try:
            # Run on port 4416 (default)
            pot_cmd = ["node", po_script]
            pot_process = subprocess.Popen(
                pot_cmd, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
            print("✅ Node.js PO Token server started on port 4416.")
            
            # Ensure it shuts down when the bot exits
            atexit.register(lambda: pot_process.terminate() if pot_process else None)
        except Exception as e:
            print(f"⚠️ Failed to start Node.js PO Token server: {e}")
            
    print("🚀 Starting aria2c RPC daemon...")
    try:
        aria_cmd = [
            "aria2c",
            "--enable-rpc",
            "--rpc-listen-all=true",
            "--rpc-allow-origin-all=true",
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--max-overall-download-limit=0",
            "--file-allocation=none",
            "--max-concurrent-downloads=100",
            "-D"
        ]
        subprocess.Popen(aria_cmd)
        print("✅ aria2c daemon started with optimized high-concurrency flags.")
    except Exception as e:
        print(f"⚠️ Failed to start aria2c daemon: {e}")

    # Start Flask health server in background thread (required by Koyeb)
    # Health check returns 503 until bot is fully connected (see app.py)
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print("🌐 Health server started on port 8080 (returning 503 until bot is ready)")

    # ── Lifecycle: start → mark healthy → idle → shutdown ────────────────
    async def main():
        print("🔧 Initializing main coroutine...")
        # Try to connect and verify identity
        print("🔗 Connecting bot client...")
        await bot_client.start()
        print("✅ Bot client started.")
        
        try:
            me = await bot_client.get_me()
            print(f"✅ Logged in as: @{me.username}")
        except Exception as e:
            print(f"⚠️ Could not get bot info: {e}")

        # Capture the active asyncio loop so Flask threads can dispatch tasks to it
        print("🌀 Capturing event loop...")
        from app import app as flask_app, prune_progress_task
        flask_app.bot_loop = asyncio.get_running_loop()

        # Start the background pruning task
        asyncio.create_task(prune_progress_task())
        print("🧹 Progress pruning task started.")

        # Mark health check as ready — Koyeb now routes traffic here
        flask_app.is_ready = True
        print("🎊 BOT IS ALIVE 🎊 (health check → 200)")

        # Use Pyrogram's own idle() — handles SIGTERM/SIGINT properly
        await idle()

        # Signal received — mark as shutting down
        print("👋 Bot stopping cleanly. Goodbye!")
        await bot_client.stop()

    # Run everything manually since we want more control over start/stop
    print("🎬 Starting event loop...")
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except Exception as e:
        print(f"❌ Bot crashed: {e}")
        import traceback
        traceback.print_exc()
