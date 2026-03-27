import asyncio
import os
import re
import time
import urllib.parse
import mimetypes
import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto
from plugins.config import Config
from utils.shared import bot_client
from plugins.helper.database import add_user, get_user, update_user, is_banned, is_premium_user, check_daily_limit, increment_download_count, get_user_stats, set_premium_user, get_watermark_settings, update_watermark_settings, reset_watermark_settings
from plugins.helper.upload import (
    download_url, upload_file, humanbytes,
    smart_output_name, is_ytdlp_url, fetch_ytdlp_title,
    fetch_ytdlp_formats, get_best_filename, resolve_url,
    get_file_category, probe_content_type
)

# ─────────────────────────────────────────────────────────────────────────────
# State dicts
#   PENDING_RENAMES: waiting for user to provide new filename
#   PENDING_MODE:    filename resolved, waiting for Media vs Document choice
#   PENDING_FORMATS: filename resolved, waiting for quality choice (yt-dlp only)
# ─────────────────────────────────────────────────────────────────────────────
PENDING_RENAMES: dict[int, dict] = {}   # {user_id: {"url": str, "orig": str}}
PENDING_MODE: dict[int, dict] = {}      # {user_id: {"url": str, "filename": str, "format_id": str}}
PENDING_FORMATS: dict[int, dict] = {}   # {user_id: {"url": str, "filename": str}}
ACTIVE_TASKS: dict[int, asyncio.Task] = {} # {user_id: Task}


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_filename(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = os.path.basename(parsed.path.rstrip("/"))
    return urllib.parse.unquote(name) if name else "downloaded_file"


HELP_TEXT = """
📋 **Bot Commands**

➤ /start – Start the bot 🔔
➤ /help – Show this help message ❓
➤ /about – Info about the bot ℹ️
➤ /upload `<url>` – Upload a file from a direct URL 📤
➤ /skip – Keep original filename (use after /upload)

**Caption:**
➤ /caption `<text>` – Set a custom caption for uploads 📝
➤ /showcaption – View your current caption
➤ /clearcaption – Remove your custom caption

**Thumbnail:**
➤ /setthumb – Reply to a photo to set thumbnail 🖼️
➤ /showthumb – View your current thumbnail
➤ /delthumb – Delete your saved thumbnail

**Status:**
➤ /status – View your daily download stats 📊

**Premium (⭐) Watermark:**
➤ /setwatermark `<setting> <value>` – Configure watermark 💧
➤ /showwatermark – View watermark settings
➤ /delwatermark – Reset watermark

**Admin only:**
➤ /broadcast `<msg>` – Broadcast to all users 📢
➤ /total – Total registered users 👥
➤ /ban `<id>` – Ban a user ⛔
➤ /unban `<id>` – Unban a user ✅
➤ /premium `<id>` – Toggle premium status ⭐
➤ /statusall – Bot resource usage 🚀

**Supported platforms:**
Instagram · Twitter/X · TikTok · Facebook · Reddit
Vimeo · Dailymotion · Twitch · SoundCloud · Bilibili + more
"""

ABOUT_TEXT = """
🤖 **URL Uploader Bot**

Upload files up to **2 GB** directly to Telegram from any direct URL.

**Features:**
• ✏️ Rename files before upload
• 🎬 Choose Media or Document upload mode
• 🖼️ Permanent thumbnails (saved to your account)
• 📝 Custom captions
• 📊 Live progress bars
• ⚡ Fast downloads with concurrent connections
• 💧 **Premium Watermark** - Add custom watermarks to thumbnails ⭐

**Daily Limits:**
• Free users: **50 downloads/day**
• Premium users: **Unlimited** ⭐

**Tech:** Pyrogram MTProto · MongoDB · Docker · Koyeb
"""


# ─────────────────────────────────────────────────────────────────────────────
#  Build the Mode-selection keyboard
# ─────────────────────────────────────────────────────────────────────────────

def mode_keyboard(user_id: int, document_only: bool = False) -> InlineKeyboardMarkup:
    if document_only:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Upload as Document", callback_data=f"mode:{user_id}:doc")]
        ])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Media", callback_data=f"mode:{user_id}:media"),
            InlineKeyboardButton("📄 Document", callback_data=f"mode:{user_id}:doc"),
        ]
    ])


def quality_keyboard(user_id: int, formats: list) -> InlineKeyboardMarkup:
    """Build a keyboard for selecting video resolution with quality info."""
    buttons = []
    # Add resolutions in rows of 2
    for i in range(0, len(formats), 2):
        row = []
        for f in formats[i:i+2]:
            size_val = f.get('filesize', 0)
            size_str = humanbytes(size_val) if size_val and size_val > 0 else "Unknown"
            
            # Show audio indicator and bitrate
            has_audio = f.get('has_audio', True)
            bitrate = f.get('bitrate', 0)
            audio_icon = "🔊" if has_audio else "🔇"
            bitrate_str = f" {bitrate//1000}k" if bitrate else ""
            
            label = f"{f['resolution']} {audio_icon} ({size_str}{bitrate_str})"
            row.append(InlineKeyboardButton(label, callback_data=f"qual:{user_id}:{f['format_id']}"))
        buttons.append(row)
    # Add a "Best Quality" button at the end
    best_fmt = formats[0]['format_id'] if formats else "best"
    buttons.append([InlineKeyboardButton("✨ Best Quality (Auto)", callback_data=f"qual:{user_id}:best_{best_fmt}")])
    return InlineKeyboardMarkup(buttons)


async def ask_mode(target_msg: Message, user_id: int, filename: str, document_only: bool = False):
    """Edit or reply with the upload-mode selection prompt."""
    text = (
        f"📁 **File:** `{filename}`\n\n"
        "How should this file be uploaded?"
    )
    try:
        await target_msg.edit_text(text, reply_markup=mode_keyboard(user_id, document_only))
    except Exception:
        await target_msg.reply_text(text, reply_markup=mode_keyboard(user_id, document_only), quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Core upload executor
# ─────────────────────────────────────────────────────────────────────────────

async def do_upload(
    client: Client,
    reply_to: Message,
    user_id: int,
    url: str,
    filename: str,
    force_document: bool = False,
    format_id: str = None,
):
    """Wrapper to run the actual upload logic as a cancellable task."""
    cancel_ref = [False]
    task = asyncio.create_task(_do_upload_logic(
        client, reply_to, user_id, url, filename, cancel_ref, force_document, format_id
    ))
    # Combine task and cancel_ref so we can trigger True upon cancel button press
    ACTIVE_TASKS[user_id] = (task, cancel_ref)
    try:
        await task
    except asyncio.CancelledError:
        # Cleanup is handled in finally block or within _do_upload_logic
        pass
    finally:
        ACTIVE_TASKS.pop(user_id, None)


async def _do_upload_logic(
    client: Client,
    reply_to: Message,         # message to reply status updates into
    user_id: int,              # real user id (NOT from reply_to.from_user)
    url: str,
    filename: str,
    cancel_ref: list,
    force_document: bool = False,
    format_id: str = None,
):
    status_msg = reply_to
    
    Config.LOGGER.info(f"_do_upload_logic starting for {user_id}")

    start_time = [time.time()]
    file_path = None
    try:
        Config.LOGGER.info(f"calling download_url for {user_id}")
        file_path, mime = await download_url(url, filename, status_msg, start_time, user_id, format_id=format_id, cancel_ref=cancel_ref)
        file_size = os.path.getsize(file_path)

        await increment_download_count(user_id)

        user_data = await get_user(user_id) or {}
        custom_caption = user_data.get("caption") or ""
        thumb_file_id = user_data.get("thumb") or None

        caption = custom_caption or os.path.basename(file_path)

        await status_msg.edit_text("📤 Uploading to Telegram…")
        await upload_file(
            client, reply_to.chat.id, file_path, mime,
            caption, thumb_file_id, status_msg, start_time,
            user_id=user_id,
            force_document=force_document,
            cancel_ref=cancel_ref,
        )
        await status_msg.edit_text("✅ Upload complete!")

        if Config.LOG_CHANNEL:
            elapsed = time.time() - start_time[0]
            try:
                await client.send_message(
                    Config.LOG_CHANNEL,
                    f"📤 **Upload log**\n"
                    f"👤 `{user_id}`\n"
                    f"🔗 `{url}`\n"
                    f"📁 `{os.path.basename(file_path)}`\n"
                    f"💾 {humanbytes(file_size)} · ⏱ {elapsed:.1f}s\n"
                    f"📦 Mode: {'Document' if force_document else 'Media'}\n"
                    f"🎯 Format: {format_id or 'Auto'}",
                )
            except Exception:
                pass

    except asyncio.CancelledError:
        try:
            await status_msg.edit_text("❌ **Process cancelled by user.**")
        except Exception:
            pass
    except ValueError as e:
        await status_msg.edit_text(f"❌ {e}")
    except Exception as e:
        Config.LOGGER.exception("Upload error")
        await status_msg.edit_text(f"❌ Error: `{e}`")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
#  Shared rename resolver — called after filename is decided
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_rename(
    client: Client,
    prompt_msg: Message,   # the bot's rename prompt message
    user_id: int,
    url: str,
    filename: str,
):
    """Proceed to quality selection (if yt-dlp) or mode selection."""
    # Try local yt-dlp first
    is_supported = is_ytdlp_url(url)
    res = None
    
    if is_supported:
        try:
            await prompt_msg.edit_text("🔍 **Analyzing available qualities…**")
            res = await fetch_ytdlp_formats(url)
        except Exception:
            pass
    
    # If not supported or local fetch failed, try link-api/local extractor fallback
    if not res or not res.get("formats"):
        try:
            if not is_supported:
                await prompt_msg.edit_text("🔍 **Analyzing via External Engine…**")
            # fetch_ytdlp_formats already has local extractor fallback built-in
            res = await fetch_ytdlp_formats(url)
        except Exception:
            pass

    if res and res.get("formats"):
        formats = res.get("formats")
        PENDING_FORMATS[user_id] = {"url": url, "filename": filename}
        try:
            await prompt_msg.edit_text(
                f"🎬 **Select Resolution:**\n`{filename}`",
                reply_markup=quality_keyboard(user_id, formats)
            )
            return
        except Exception:
            pass

    # Fallback/Direct loop: probe URL to detect file type
    from plugins.helper.upload import get_file_category, probe_content_type
    
    # Probe content type for accurate extension detection
    mime = await probe_content_type(url)
    file_category = get_file_category(url, mime)
    
    # For non-media files (archive, document, unknown), show document-only option
    document_only = file_category not in ('video', 'audio', 'image')
    
    # If it's an image, try to get accurate extension from mime
    if file_category == 'image' and mime:
        ext = mimetypes.guess_extension(mime.split(';')[0].strip())
        if ext and ext != '.jpe':
            base_name = os.path.splitext(filename)[0]
            filename = base_name + ext
    
    PENDING_MODE[user_id] = {"url": url, "filename": filename, "format_id": None}
    await ask_mode(prompt_msg, user_id, filename, document_only)


# ─────────────────────────────────────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user = message.from_user
    await add_user(user.id, user.username)
    if await is_banned(user.id):
        return await message.reply_text("🚫 You are banned from using this bot.")

    buttons = []
    if Config.UPDATES_CHANNEL:
        buttons.append([InlineKeyboardButton("📢 Updates", url=f"https://t.me/{Config.UPDATES_CHANNEL}")])
    buttons.append([InlineKeyboardButton("❓ Help", callback_data="help"),
                    InlineKeyboardButton("ℹ️ About", callback_data="about")])
    kb = InlineKeyboardMarkup(buttons)
    
    is_prem = await is_premium_user(user.id)
    stats = await get_user_stats(user.id)
    
    premium_text = "⭐ **Premium User**" if is_prem else f"📊 **Downloads:** {stats['remaining']} remaining today"
    
    welcome_text = (
        f"👋 Hello **{user.first_name}**!\n\n"
        f"**{premium_text}**\n\n"
        "Send me a **direct URL** or use `/upload <url>` to upload files up to **2 GB** to Telegram.\n\n"
        "Supported: Instagram, Twitter/X, TikTok, Reddit, Facebook + more."
    )

    await message.reply_text(
        welcome_text,
        reply_markup=kb,
        quote=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /help  /about
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("help") & filters.private)
async def help_handler(client: Client, message: Message):
    await message.reply_text(HELP_TEXT, quote=True)


@Client.on_message(filters.command("about") & filters.private)
async def about_handler(client: Client, message: Message):
    await message.reply_text(ABOUT_TEXT, quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Inline keyboard callbacks  — MUST use specific filters to avoid conflicts
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^(help|about)$"))
async def cb_help_about(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    if data == "help":
        await callback_query.message.edit_text(HELP_TEXT)
    elif data == "about":
        await callback_query.message.edit_text(ABOUT_TEXT)
    await callback_query.answer()


@Client.on_callback_query(filters.regex(r"^qual:(\d+):(.+)$"))
async def cb_quality(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    parts = callback_query.data.split(":")
    target_id = int(parts[1])
    format_id = parts[2]  # "best" or specific id

    if user_id != target_id:
        return await callback_query.answer("Not your upload!", show_alert=True)

    pending = PENDING_FORMATS.pop(user_id, None)
    if not pending:
        return await callback_query.answer("Already processed or expired.", show_alert=True)

    await callback_query.answer()
    
    if format_id.startswith("best_"):
        chosen_label = "Best (Auto)"
        format_id = "best"
    else:
        chosen_label = format_id

    try:
        await callback_query.message.edit_text(f"✅ Quality: **{chosen_label}**")
    except Exception:
        pass
    
    # Store choice and move to mode selection
    PENDING_MODE[user_id] = {
        "url": pending["url"],
        "filename": pending["filename"],
        "format_id": format_id
    }
    await ask_mode(callback_query.message, user_id, pending["filename"])


@Client.on_callback_query(filters.regex(r"^cancel:(\d+)$"))
async def cb_cancel(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    target_id = int(callback_query.data.split(":")[1])

    if user_id != target_id:
        return await callback_query.answer("Not your process!", show_alert=True)

    task_info = ACTIVE_TASKS.get(user_id)
    if not task_info:
        await callback_query.answer("No active process to cancel.", show_alert=True)
        try:
            await callback_query.message.edit_text("❌ **Process already finished or cancelled.**")
        except Exception:
            pass
        return

    try:
        task, cancel_ref = task_info
        cancel_ref[0] = True  # Signal to abort
        
        # Try multiple cancellation methods
        if not task.done():
            task.cancel()
        
        # Also try to cancel via asyncio
        try:
            asyncio.get_event_loop().call_later(0.5, lambda: ACTIVE_TASKS.pop(user_id, None))
        except Exception:
            pass
            
        ACTIVE_TASKS.pop(user_id, None)
        await callback_query.answer("Process cancelled!")
        try:
            await callback_query.message.edit_text("✅ **Process cancelled successfully.**")
        except Exception:
            pass
    except Exception as e:
        await callback_query.answer("Error cancelling process.", show_alert=True)
        Config.LOGGER.warning(f"Cancel error: {e}")


@Client.on_callback_query(filters.regex(r"^skip_rename:(\d+)$"))
async def skip_rename_cb(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    target_id = int(callback_query.data.split(":")[1])
    if user_id != target_id:
        return await callback_query.answer("Not your upload!", show_alert=True)

    pending = PENDING_RENAMES.pop(user_id, None)
    if not pending:
        return await callback_query.answer("Already processed or expired.", show_alert=True)

    await callback_query.answer()
    # Move to mode selection
    await resolve_rename(
        client,
        callback_query.message,   # the rename prompt message to edit in place
        user_id,
        pending["url"],
        pending["orig"],
    )


@Client.on_callback_query(filters.regex(r"^mode:(\d+):(media|doc)$"))
async def mode_cb(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    parts = callback_query.data.split(":")
    target_id = int(parts[1])
    choice = parts[2]   # "media" or "doc"

    if user_id != target_id:
        return await callback_query.answer("Not your upload!", show_alert=True)

    pending = PENDING_MODE.pop(user_id, None)
    if not pending:
        return await callback_query.answer("Already processed or expired.", show_alert=True)

    await callback_query.answer()
    mode_label = "📄 Document" if choice == "doc" else "🎬 Media"
        
    try:
        await callback_query.message.edit_text(
            f"✅ Uploading as **{mode_label}**…\n`{pending['filename']}`"
        )
    except Exception:
        pass

    await do_upload(
        client,
        callback_query.message,
        user_id,
        pending["url"],
        pending["filename"],
        force_document=(choice == "doc"),
        format_id=pending.get("format_id"),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /upload <url>  — step 1: ask for rename
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("upload") & filters.private)
async def upload_handler(client: Client, message: Message):
    user = message.from_user
    await add_user(user.id, user.username)

    if await is_banned(user.id):
        return await message.reply_text("🚫 You are banned.")

    is_prem = await is_premium_user(user.id)
    if not is_prem and user.id != Config.OWNER_ID and user.id not in Config.ADMIN:
        can_download, remaining = await check_daily_limit(user.id)
        if not can_download:
            return await message.reply_text(
                "⚠️ **Daily limit reached!**\n\n"
                "You've used all 50 downloads for today.\n\n"
                "🌟 **Upgrade to Premium for unlimited downloads!**\n\n"
                "Contact: @premiumdownloaderinfobot",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌟 Get Premium", url="https://t.me/premiumdownloaderinfobot")]
                ]),
                quote=True
            )

    args = message.command

    url = None
    if len(args) > 1:
        url = args[1].strip()
    elif message.reply_to_message and message.reply_to_message.text:
        url = message.reply_to_message.text.strip()

    if not url or not url.startswith(("http://", "https://")):
        return await message.reply_text(
            "❌ Please provide a valid direct URL.\n\nUsage: `/upload https://example.com/file.mp4`",
            quote=True,
        )

    if "youtube.com" in url.lower() or "youtu.be" in url.lower():
        return await message.reply_text(
            "❌ **YouTube downloads are not supported.**\n\n"
            "Please provide a direct media URL from other platforms.",
            quote=True,
        )

    status_info = await message.reply_text("🔍 Analyzing file info…", quote=True)
    try:
        url = await resolve_url(url)
    except Exception:
        pass

    # For yt-dlp URLs, fetch the video title to use as suggested filename
    if is_ytdlp_url(url):
        try:
            await status_info.edit_text("🔍 Fetching video info…")
        except Exception:
            pass
        fetched = await fetch_ytdlp_title(url)
        try:
            await status_info.delete()
        except Exception:
            pass
        orig_filename = fetched or smart_output_name(extract_filename(url))
    else:
        orig_filename = smart_output_name(extract_filename(url))
        try:
            await status_info.delete()
        except Exception:
            pass
    PENDING_RENAMES[user.id] = {"url": url, "orig": orig_filename}

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip (keep original)", callback_data=f"skip_rename:{user.id}")]
    ])
    await message.reply_text(
        f"✏️ **Rename file?**\n\n"
        f"📁 Original: `{orig_filename}`\n\n"
        "Send the **new filename** (with extension) or press **Skip**:",
        reply_markup=kb,
        quote=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /skip — keep original filename via command
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("skip") & filters.private)
async def skip_handler(client: Client, message: Message):
    user_id = message.from_user.id
    pending = PENDING_RENAMES.pop(user_id, None)
    if not pending:
        return await message.reply_text("❌ No pending upload. Send a URL first.", quote=True)

    prompt = await message.reply_text("⏭ Keeping original filename…", quote=True)
    await resolve_rename(client, prompt, user_id, pending["url"], pending["orig"])


# ─────────────────────────────────────────────────────────────────────────────
#  Text handler — rename input OR bare URL
# ─────────────────────────────────────────────────────────────────────────────

_ALL_COMMANDS = [
    "start", "help", "about", "upload", "skip", "caption", "showcaption",
    "clearcaption", "setthumb", "showthumb", "delthumb",
    "broadcast", "total", "ban", "unban", "status",
]


@Client.on_message(filters.private & filters.text & ~filters.command(_ALL_COMMANDS))
async def text_handler(client: Client, message: Message):
    user = message.from_user
    text = (message.text or "").strip()

    # ── Pending rename input ──────────────────────────────────────────────────
    if user.id in PENDING_RENAMES:
        pending = PENDING_RENAMES.pop(user.id)
        new_name = text.strip()
        # Preserve original extension if user didn't include one
        orig_ext = os.path.splitext(pending["orig"])[1]
        new_ext = os.path.splitext(new_name)[1]
        if not new_ext and orig_ext:
            new_name = new_name + orig_ext

        prompt = await message.reply_text(f"✏️ Renamed to: `{new_name}`", quote=True)
        await resolve_rename(client, prompt, user.id, pending["url"], new_name)
        return

    # ── Bare URL ──────────────────────────────────────────────────────────────
    if text.startswith(("http://", "https://")):
        await add_user(user.id, user.username)
        if await is_banned(user.id):
            return await message.reply_text("🚫 You are banned.")
        
        is_prem = await is_premium_user(user.id)
        if not is_prem and user.id != Config.OWNER_ID and user.id not in Config.ADMIN:
            can_download, remaining = await check_daily_limit(user.id)
            if not can_download:
                return await message.reply_text(
                    "⚠️ **Daily limit reached!**\n\n"
                    "You've used all 50 downloads for today.\n\n"
                    "🌟 **Upgrade to Premium for unlimited downloads!**\n\n"
                    "Contact: @premiumdownloaderinfobot",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🌟 Get Premium", url="https://t.me/premiumdownloaderinfobot")]
                    ]),
                    quote=True
                )

        if "youtube.com" in text.lower() or "youtu.be" in text.lower():
            return await message.reply_text(
                "❌ **YouTube downloads are not supported.**\n\n"
                "Please provide a direct media URL from other platforms.",
                quote=True,
            )

        status_info = await message.reply_text("🔍 Analyzing file info…", quote=True)
        try:
            text = await resolve_url(text)
        except Exception:
            pass
        
        orig_filename = await get_best_filename(text)
        try:
            await status_info.delete()
        except Exception:
            pass
            
        PENDING_RENAMES[user.id] = {"url": text, "orig": orig_filename}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Skip (keep original)", callback_data=f"skip_rename:{user.id}")]
        ])
        await message.reply_text(
            f"✏️ **Rename file?**\n\n"
            f"📁 Original: `{orig_filename}`\n\n"
            "Send the **new filename** (with extension) or press **Skip**:",
            reply_markup=kb,
            quote=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Caption management
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("caption") & filters.private)
async def set_caption(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply_text("Usage: `/caption Your caption text here`", quote=True)
    caption = " ".join(args[1:])
    await update_user(message.from_user.id, {"caption": caption})
    await message.reply_text(f"✅ Caption saved:\n\n{caption}", quote=True)


@Client.on_message(filters.command("showcaption") & filters.private)
async def show_caption(client: Client, message: Message):
    user_data = await get_user(message.from_user.id) or {}
    cap = user_data.get("caption") or "_(none set)_"
    await message.reply_text(f"📝 Your caption:\n\n{cap}", quote=True)


@Client.on_message(filters.command("clearcaption") & filters.private)
async def clear_caption(client: Client, message: Message):
    await update_user(message.from_user.id, {"caption": ""})
    await message.reply_text("✅ Caption cleared.", quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Thumbnail management — stored as Telegram file_id (permanent)
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("setthumb") & filters.private)
async def set_thumb(client: Client, message: Message):
    reply = message.reply_to_message
    if not reply or not reply.photo:
        return await message.reply_text(
            "❌ Reply to a **photo** with /setthumb to save it as your thumbnail.",
            quote=True,
        )
    file_id = reply.photo.file_id
    await update_user(message.from_user.id, {"thumb": file_id})
    await message.reply_text(
        "✅ Thumbnail saved permanently!\n"
        "It will be applied to all your future uploads.",
        quote=True,
    )


@Client.on_message(filters.command("showthumb") & filters.private)
async def show_thumb(client: Client, message: Message):
    user_data = await get_user(message.from_user.id) or {}
    thumb_id = user_data.get("thumb")
    if not thumb_id:
        return await message.reply_text("❌ No thumbnail set. Reply to a photo with /setthumb.", quote=True)
    try:
        await message.reply_photo(photo=thumb_id, caption="🖼️ Your current thumbnail", quote=True)
    except Exception as e:
        await message.reply_text(f"❌ Could not show thumbnail: `{e}`", quote=True)


@Client.on_message(filters.command("delthumb") & filters.private)
async def del_thumb(client: Client, message: Message):
    await update_user(message.from_user.id, {"thumb": None})
    await message.reply_text("✅ Thumbnail deleted.", quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  /status - User download stats
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("status") & filters.private)
async def user_status(client: Client, message: Message):
    user_id = message.from_user.id
    
    is_prem = await is_premium_user(user_id)
    stats = await get_user_stats(user_id)
    
    if is_prem:
        text = (
            "📊 **Your Status**\n\n"
            "⭐ **Premium User**\n"
            "📥 **Downloads:** Unlimited"
        )
    else:
        remaining = stats['remaining']
        used = 50 - remaining
        text = (
            "📊 **Your Status**\n\n"
            "👤 **Free User**\n"
            f"📥 **Downloads today:** {used}/50\n"
            f"⏳ **Remaining:** {remaining}"
        )
    
    await message.reply_text(text, quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Watermark Commands (Premium Only)
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("setwatermark") & filters.private)
async def set_watermark(client: Client, message: Message):
    try:
        user_id = message.from_user.id
        await add_user(user_id, message.from_user.username)
        
        if await is_banned(user_id):
            return await message.reply_text("🚫 You are banned.", quote=True)
        
        if not await is_premium_user(user_id) and user_id != Config.OWNER_ID and user_id not in Config.ADMIN:
            return await message.reply_text(
                "🌟 **Premium Feature**\n\n"
                "Watermark is only available for **Premium users**.\n\n"
                "Contact: @premiumdownloaderinfobot",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌟 Get Premium", url="https://t.me/premiumdownloaderinfobot")]
                ]),
                quote=True
            )
        
        args = message.command[1:]
        
        if not args:
            return await message.reply_text(
                "💧 **Watermark Settings**\n\n"
                "**Usage:** `/setwatermark <setting> <value>`\n\n"
                "**Settings:**\n"
                "`text <text>` - Watermark text (max 50 chars)\n"
                "`position <pos>` - Position: top-left, top-right, bottom-left, bottom-right, center, top-center, bottom-center\n"
                "`size <8-200>` - Font size\n"
                "`opacity <0.1-1.0>` - Text opacity\n"
                "`angle <-180 to 180>` - Rotation angle\n"
                "`shadow on/off` - Enable/disable shadow\n"
                "`enable on/off` - Enable/disable watermark\n\n"
                "**Example:**\n"
                "`/setwatermark text MYCHANNEL`\n"
                "`/setwatermark position bottom-right`\n"
                "`/setwatermark size 32`\n\n"
                "**View current settings:** `/showwatermark`\n"
                "**Reset to default:** `/delwatermark`",
                quote=True
            )
        
        settings = await get_watermark_settings(user_id)
        
        cmd = args[0].lower()
        value = " ".join(args[1:])
        
        feedback = []
        
        if cmd == "text":
            if len(value) > 50:
                return await message.reply_text("❌ Text must be 50 characters or less.", quote=True)
            settings["text"] = value
            feedback.append(f"📝 Text: `{value}`")
            
        elif cmd == "position":
            valid_positions = ["top-left", "top-right", "bottom-left", "bottom-right", "center", "top-center", "bottom-center"]
            if value.lower() not in valid_positions:
                return await message.reply_text(
                    f"❌ Invalid position. Choose: {', '.join(valid_positions)}",
                    quote=True
                )
            settings["position"] = value.lower()
            feedback.append(f"📍 Position: `{value.lower()}`")
            
        elif cmd == "size":
            try:
                size = int(value)
                if size < 8 or size > 200:
                    return await message.reply_text("❌ Size must be 8-200.", quote=True)
                settings["font_size"] = size
                feedback.append(f"🔤 Size: `{size}`")
            except ValueError:
                return await message.reply_text("❌ Size must be a number.", quote=True)
                
        elif cmd == "opacity":
            try:
                opacity = float(value)
                if opacity < 0.1 or opacity > 1.0:
                    return await message.reply_text("❌ Opacity must be 0.1-1.0.", quote=True)
                settings["opacity"] = opacity
                feedback.append(f"💨 Opacity: `{opacity}`")
            except ValueError:
                return await message.reply_text("❌ Opacity must be a number.", quote=True)
                
        elif cmd == "angle":
            try:
                angle = int(value)
                if angle < -180 or angle > 180:
                    return await message.reply_text("❌ Angle must be -180 to 180.", quote=True)
                settings["angle"] = angle
                feedback.append(f"🔄 Angle: `{angle}°`")
            except ValueError:
                return await message.reply_text("❌ Angle must be a number.", quote=True)
                
        elif cmd == "shadow":
            if value.lower() == "on":
                settings["shadow"] = True
                feedback.append("🌑 Shadow: **ON**")
            elif value.lower() == "off":
                settings["shadow"] = False
                feedback.append("🌑 Shadow: **OFF**")
            else:
                return await message.reply_text("❌ Use `on` or `off`.", quote=True)
                
        elif cmd == "enable":
            if value.lower() == "on":
                settings["enabled"] = True
                feedback.append("✅ Watermark: **ENABLED**")
            elif value.lower() == "off":
                settings["enabled"] = False
                feedback.append("🚫 Watermark: **DISABLED**")
            else:
                return await message.reply_text("❌ Use `on` or `off`.", quote=True)
                
        elif cmd == "color":
            color_map = {
                "white": [255, 255, 255, 200],
                "black": [0, 0, 0, 200],
                "red": [255, 0, 0, 200],
                "green": [0, 255, 0, 200],
                "blue": [0, 0, 255, 200],
                "yellow": [255, 255, 0, 200],
                "cyan": [0, 255, 255, 200],
                "magenta": [255, 0, 255, 200],
            }
            if value.lower() in color_map:
                settings["color"] = color_map[value.lower()]
                feedback.append(f"🎨 Color: **{value.lower()}**")
            else:
                return await message.reply_text(
                    f"❌ Invalid color. Choose: {', '.join(color_map.keys())}",
                    quote=True
                )
        else:
            return await message.reply_text(f"❌ Unknown setting: `{cmd}`", quote=True)
        
        await update_watermark_settings(user_id, settings)
        
        text = "✅ **Watermark Updated!**\n\n" + "\n".join(feedback)
        await message.reply_text(text, quote=True)
        
        if settings.get("enabled"):
            try:
                from plugins.helper.watermark import generate_preview
                preview = generate_preview(settings)
                if preview:
                    await message.reply_photo(
                        photo=preview,
                        caption="👀 **Preview** - This is how your watermark will look.",
                        quote=True
                    )
            except Exception:
                pass
    except Exception as e:
        Config.LOGGER.error(f"set_watermark error: {e}")
        await message.reply_text(f"Error: {e}", quote=True)


@Client.on_message(filters.command("showwatermark") & filters.private)
async def show_watermark(client: Client, message: Message):
    try:
        user_id = message.from_user.id
        
        if await is_banned(user_id):
            return await message.reply_text("🚫 You are banned.", quote=True)
        
        settings = await get_watermark_settings(user_id)
        
        status = "✅ **Enabled**" if settings.get("enabled") else "🚫 **Disabled**"
        
        text = (
            "💧 **Your Watermark Settings**\n\n"
            f"**Status:** {status}\n"
            f"**Text:** `{settings.get('text', 'PREMIUM')}`\n"
            f"**Position:** `{settings.get('position', 'bottom-right')}`\n"
            f"**Font Size:** `{settings.get('font_size', 24)}`\n"
            f"**Opacity:** `{settings.get('opacity', 1.0)}`\n"
            f"**Angle:** `{settings.get('angle', 0)}°`\n"
            f"**Shadow:** {'**ON**' if settings.get('shadow') else '**OFF**'}\n\n"
            "**Change settings:** `/setwatermark <setting> <value>`\n"
            "**Reset:** `/delwatermark`"
        )
        
        await message.reply_text(text, quote=True)
        
        if settings.get("enabled"):
            try:
                from plugins.helper.watermark import generate_preview
                preview = generate_preview(settings)
                if preview:
                    await message.reply_photo(
                        photo=preview,
                        caption="👀 **Preview**",
                        quote=True
                    )
            except Exception:
                pass
    except Exception as e:
        Config.LOGGER.error(f"show_watermark error: {e}")
        await message.reply_text(f"Error: {e}", quote=True)


@Client.on_message(filters.command("delwatermark") & filters.private)
async def del_watermark(client: Client, message: Message):
    try:
        user_id = message.from_user.id
        
        if await is_banned(user_id):
            return await message.reply_text("🚫 You are banned.", quote=True)
        
        if not await is_premium_user(user_id) and user_id != Config.OWNER_ID and user_id not in Config.ADMIN:
            return await message.reply_text(
                "🌟 **Premium Feature**\n\n"
                "Watermark is only available for **Premium users**.",
                quote=True
            )
        
        await reset_watermark_settings(user_id)
        await message.reply_text("✅ Watermark settings reset to default.", quote=True)
    except Exception as e:
        Config.LOGGER.error(f"del_watermark error: {e}")
        await message.reply_text(f"Error: {e}", quote=True)
