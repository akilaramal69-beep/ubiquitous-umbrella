import asyncio
import os
import re
import time
import urllib.parse
import mimetypes
import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from plugins.config import Config
from utils.shared import bot_client
from plugins.helper.database import (
    add_user, get_user, update_user, is_banned, is_premium_user,
    check_daily_limit, increment_download_count, get_user_stats,
    set_premium_user, get_watermark, set_watermark, clear_watermark,
    set_watermark_image, update_watermark_field, get_subtitle_settings
)
from utils.subtitles import generate_subtitles, burn_subtitles
from utils.shared import SUBTITLE_STATES
from plugins.helper.upload import (
    download_url, upload_file, humanbytes,
    smart_output_name, is_ytdlp_url, fetch_ytdlp_title,
    fetch_ytdlp_formats, get_best_filename, resolve_url,
    get_file_category, probe_content_type, VALID_POSITIONS
)

# ─────────────────────────────────────────────────────────────────────────────
# State dicts
#   PENDING_RENAMES: waiting for user to provide new filename
#   PENDING_MODE:    filename resolved, waiting for Media vs Document choice
#   PENDING_FORMATS: filename resolved, waiting for quality choice (yt-dlp only)
# ─────────────────────────────────────────────────────────────────────────────
PENDING_RENAMES: dict[int, dict] = {}   # {user_id: {"url": str, "media_msg_id": int, "orig": str, "custom_thumb": str}}
PENDING_THUMBNAILS: dict[int, dict] = {}# {user_id: {"url": str, "media_msg_id": int, "orig": str}}
PENDING_MODE: dict[int, dict] = {}      # {user_id: {"url": str, "media_msg_id": int, "filename": str, "format_id": str, "custom_thumb": str}}
PENDING_FORMATS: dict[int, dict] = {}   # {user_id: {"url": str, "media_msg_id": int, "filename": str, "custom_thumb": str}}
ACTIVE_TASKS: dict[int, asyncio.Task] = {} # {user_id: Task}

_ALL_COMMANDS = [
    "start", "help", "about", "upload", "skip", "caption", "showcaption",
    "clearcaption", "setthumb", "showthumb", "delthumb", "setwatermark",
    "wmcolor", "wmopacity", "wmsize", "wmpos", "showwatermark", "clearwatermark",
    "broadcast", "total", "ban", "unban", "premium", "statusall",
    "setsubs", "sublang", "submethod", "submodel", "substats"
]


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

**Premium Features ⭐:**
➤ /setwatermark `<text> [pos]` or reply to photo – Set watermark
➤ /wmcolor `<hex>` – Set text color (e.g. #ffffff)
➤ /wmopacity `<0-100>` – Set opacity
➤ /wmsize `<1-100>` – Set size percentage
➤ /showwatermark – View current watermark settings
➤ /clearwatermark – Remove watermark
➤ /setsubs `<on/off>` – Toggle subtitle generation 📝
➤ /sublang `<lang>` – Set subtitle language (e.g. en, ja, auto)
➤ /submethod `<local/api>` – Toggle AI method (Local is free, API is faster/accurate)
➤ /submodel `<base/small>` – Set local AI model
➤ /substats – View your current subtitle settings

**Status:**
➤ /status – View your daily download stats 📊

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
• 📝 **AI Video Subtitle Generation** (Premium) ⭐

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
    url: str = None,
    filename: str = None,
    force_document: bool = False,
    format_id: str = None,
    custom_thumb: str = None,
    media_msg_id: int = None,
):
    """Wrapper to run the actual upload logic as a cancellable task."""
    cancel_ref = [False]
    task = asyncio.create_task(_do_upload_logic(
        client, reply_to, user_id, url, filename, cancel_ref, force_document, format_id, custom_thumb, media_msg_id
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
    url: str = None,
    filename: str = None,
    cancel_ref: list = None,
    force_document: bool = False,
    format_id: str = None,
    custom_thumbnail: str = None,
    media_msg_id: int = None,
):
    status_msg = reply_to
    
    Config.LOGGER.info(f"_do_upload_logic starting for {user_id}")

    start_time = [time.time()]
    file_path = None
    try:
        if media_msg_id:
            status_msg = await status_msg.edit_text("📥 **Downloading from Telegram…**")
            # Get the media message
            media_msg = await client.get_messages(user_id, media_msg_id)
            file_path = await client.download_media(
                media_msg,
                file_name=os.path.join(Config.DOWNLOAD_LOCATION, str(user_id), filename),
                progress=progress_for_pyrogram,
                progress_args=("📥 **Downloading…**", status_msg, start_time[0])
            )
            mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        else:
            Config.LOGGER.info(f"calling download_url for {user_id}")
            file_path, mime = await download_url(url, filename, status_msg, start_time, user_id, format_id=format_id, cancel_ref=cancel_ref)
        
        file_size = os.path.getsize(file_path)

        await increment_download_count(user_id)

        user_data = await get_user(user_id) or {}
        custom_caption = user_data.get("caption") or ""
        thumb_file_id = custom_thumbnail or user_data.get("thumb") or None

        # Fetch watermark for premium users
        wm_data = None
        if user_data.get("is_premium", False):
            wm_settings = await get_watermark(user_id)
            if wm_settings.get("text") or wm_settings.get("image"):
                wm_data = wm_settings

        caption = custom_caption or os.path.splitext(os.path.basename(file_path))[0]

        await status_msg.edit_text("📤 Uploading to Telegram…")
        sent_msg = await upload_file(
            client, reply_to.chat.id, file_path, mime,
            caption, thumb_file_id, status_msg, start_time,
            user_id=user_id,
            force_document=force_document,
            cancel_ref=cancel_ref,
            watermark=wm_data,
        )
        await status_msg.edit_text("✅ Upload complete!")

        # ── Subtitle Generation (Premium Only) ──────────────────────────────
        if user_data.get("is_premium", False) and mime.startswith("video/") and not force_document:
            sub_settings = await get_subtitle_settings(user_id)
            if sub_settings.get("enabled"):
                try:
                    from utils.subtitles import generate_subtitles, get_progress_bar
                    
                    last_update_time = 0
                    async def sub_progress_cb(percent):
                        nonlocal last_update_time
                        if time.time() - last_update_time < 2 and percent < 100: return
                        last_update_time = time.time()
                        bar = get_progress_bar(percent)
                        try: await status_msg.edit_text(f"📝 **Generating AI Subtitles...**\n\n`{bar}`\n\n_(this may take a few minutes)_")
                        except: pass

                    srt_path = await generate_subtitles(
                        file_path, 
                        lang=sub_settings.get("language", "auto"),
                        method=sub_settings.get("method", "local"),
                        model=sub_settings.get("model", "base"),
                        engine=sub_settings.get("engine", "stable-ts"),
                        progress_callback=sub_progress_cb
                    )
                    
                    # Ensure status msg is clean after progress
                    await status_msg.edit_text("📝 **AI Subtitles Generated!**")
                    
                    if srt_path and os.path.exists(srt_path):
                        # ── Store state and ask user ──────────────────────────
                        state_id = f"{user_id}_{int(time.time())}"
                        SUBTITLE_STATES[state_id] = {
                            "file_path": file_path,
                            "srt_path": srt_path,
                            "mime": mime,
                            "caption": caption,
                            "thumb_file_id": thumb_file_id,
                            "start_time": start_time,
                            "user_id": user_id,
                            "force_document": force_document,
                            "wm_data": wm_data,
                            "status_msg_id": status_msg.id,
                            "sent_msg_id": sent_msg.id if sent_msg else None
                        }
                        
                        buttons = [
                            [
                                InlineKeyboardButton("📝 Send SRT + Video", callback_data=f"sub_srt|{state_id}"),
                                InlineKeyboardButton("🔥 Burn into Video", callback_data=f"sub_burn|{state_id}")
                            ]
                        ]
                        await status_msg.edit_text(
                            "✅ **Transcription Complete!**\n\nHow do you want to receive the subtitles?",
                            reply_markup=InlineKeyboardMarkup(buttons)
                        )
                        # CRITICAL: Set file_path to None so 'finally' block doesn't delete it.
                        # The callback handler will handle deletion after the user makes a choice.
                        file_path = None 
                        return 
                    else:
                        await status_msg.edit_text("✅ Upload complete! (subtitle generation skipped or failed)")
                except Exception as sub_err:
                    Config.LOGGER.error(f"Subtitle generation failed: {sub_err}")
                    await status_msg.edit_text("✅ Upload complete! (subtitles failed)")

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


async def progress_for_pyrogram(current, total, ud_type, message, start):
    now = time.time()
    diff = now - start
    if round(diff % 10.00) == 0 or current == total:
        # if diff < 0.1:
        #    return
        percentage = current * 100 / total
        speed = current / diff
        elapsed_time = round(diff) * 1000
        time_to_completion = round((total - current) / speed) * 1000
        estimated_total_time = elapsed_time + time_to_completion

        elapsed_time_str = humanbytes(elapsed_time / 1000) + "s"
        estimated_total_time_str = humanbytes(estimated_total_time / 1000) + "s"

        progress_str = progress_bar(int(percentage), 100)

        tmp = (
            f"{ud_type}\n"
            f"[{progress_str}] {round(percentage, 2)}%\n"
            f"**Done:** {humanbytes(current)} / {humanbytes(total)}\n"
            f"**Speed:** {humanbytes(speed)}/s\n"
            f"**ETA:** {humanbytes(time_to_completion / 1000)}s"
        )
        try:
            await message.edit_text(
                text=tmp,
                reply_markup=cancel_button(message.from_user.id if message.from_user else 0)
            )
        except Exception:
            pass


def progress_bar(percentage, total):
    """Simple progress bar generator."""
    completed = int(percentage / (100 / 10))
    return "⬛" * completed + "⬜" * (10 - completed)


def cancel_button(user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{user_id}")]
    ])


# ─────────────────────────────────────────────────────────────────────────────
#  Shared rename resolver — called after filename is decided
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_rename(
    client: Client,
    prompt_msg: Message,   # the bot's rename prompt message
    user_id: int,
    url: str = None,
    filename: str = None,
    custom_thumb: str = None,
    media_msg_id: int = None,
):
    """Proceed to quality selection (if yt-dlp) or mode selection."""
    # If it's forwarded media, skip quality analysis
    if media_msg_id:
        PENDING_MODE[user_id] = {
            "media_msg_id": media_msg_id,
            "filename": filename,
            "format_id": None,
            "custom_thumb": custom_thumb
        }
        await ask_mode(prompt_msg, user_id, filename)
        return

    # Try local yt-dlp first
    is_supported = is_ytdlp_url(url) if url else False
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
        PENDING_FORMATS[user_id] = {"url": url, "filename": filename, "custom_thumb": custom_thumb}
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
    
    PENDING_MODE[user_id] = {"url": url, "filename": filename, "format_id": None, "custom_thumb": custom_thumb}
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
        "url": pending.get("url"),
        "media_msg_id": pending.get("media_msg_id"),
        "filename": pending["filename"],
        "format_id": format_id,
        "custom_thumb": pending.get("custom_thumb")
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


@Client.on_callback_query(filters.regex(r"^set_thumb:(\d+)$"))
async def cb_set_thumb(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    target_id = int(callback_query.data.split(":")[1])
    if user_id != target_id:
        return await callback_query.answer("Not your upload!", show_alert=True)
    
    if not await is_premium_user(user_id):
        return await callback_query.answer("🌟 This is a Premium feature!", show_alert=True)

    if user_id not in PENDING_RENAMES:
        return await callback_query.answer("Already processed or expired.", show_alert=True)
        
    PENDING_THUMBNAILS[user_id] = PENDING_RENAMES.pop(user_id)
    await callback_query.answer()
    try:
        await callback_query.message.edit_text("🖼️ **Send Photo**\n\nPlease send a photo to use as the thumbnail for this upload.")
    except Exception:
        pass


@Client.on_message(filters.private & filters.photo & ~filters.command(_ALL_COMMANDS))
async def photo_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id in PENDING_THUMBNAILS:
        pending = PENDING_THUMBNAILS.pop(user_id)
        pending["custom_thumb"] = message.photo.file_id
        PENDING_RENAMES[user_id] = pending
        
        is_prem = await is_premium_user(user_id)
        buttons = []
        if is_prem:
            buttons.append([InlineKeyboardButton("🖼️ Set Different Thumbnail", callback_data=f"set_thumb:{user_id}")])
        buttons.append([InlineKeyboardButton("⏭ Skip (keep original)", callback_data=f"skip_rename:{user_id}")])
        kb = InlineKeyboardMarkup(buttons)
        
        await message.reply_text(
            f"✅ **Thumbnail received!**\n\n"
            f"✏️ **Rename file?**\n"
            f"📁 Original: `{pending['orig']}`\n\n"
            "Send the **new filename** (with extension) or press **Skip**:",
            reply_markup=kb,
            quote=True,
        )


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
        url=pending.get("url"),
        filename=pending["orig"],
        custom_thumb=pending.get("custom_thumb"),
        media_msg_id=pending.get("media_msg_id")
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
        url=pending.get("url"),
        filename=pending["filename"],
        force_document=(choice == "doc"),
        format_id=pending.get("format_id"),
        custom_thumb=pending.get("custom_thumb"),
        media_msg_id=pending.get("media_msg_id")
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
    
    await initiate_rename(message, user.id, orig_filename, url=url)


@Client.on_message(filters.private & (filters.video | filters.document))
async def media_handler(client: Client, message: Message):
    user = message.from_user
    await add_user(user.id, user.username)
    if await is_banned(user.id):
        return await message.reply_text("🚫 You are banned.")

    # Only process videos or documents that look like videos
    media = message.video or message.document
    if not media:
        return
        
    # If it's a document, check if it's video-related
    if message.document:
        is_video = (media.mime_type and media.mime_type.startswith("video/"))
        has_video_ext = (media.file_name and any(media.file_name.lower().endswith(x) for x in ['.mp4', '.mkv', '.mov', '.avi', '.webm', '.ts', '.m4v', '.flv']))
        if not (is_video or has_video_ext):
            return

    # Use a default filename if none exists
    orig_filename = media.file_name or (f"video_{message.id}.mp4" if message.video else "file")
    await initiate_rename(message, user.id, orig_filename, media_msg_id=message.id)


async def initiate_rename(message: Message, user_id: int, orig_filename: str, url: str = None, media_msg_id: int = None):
    """Common helper to start the rename/thumbnail prompt."""
    is_prem = await is_premium_user(user_id)
    PENDING_RENAMES[user_id] = {
        "url": url,
        "media_msg_id": media_msg_id,
        "orig": orig_filename
    }

    buttons = []
    if is_prem:
        buttons.append([InlineKeyboardButton("🖼️ Custom Thumbnail", callback_data=f"set_thumb:{user_id}")])
    buttons.append([InlineKeyboardButton("⏭ Skip (keep original)", callback_data=f"skip_rename:{user_id}")])
    kb = InlineKeyboardMarkup(buttons)

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
    await resolve_rename(
        client, prompt, user_id, 
        url=pending.get("url"), 
        filename=pending["orig"], 
        custom_thumb=pending.get("custom_thumb"),
        media_msg_id=pending.get("media_msg_id")
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Text handler — rename input OR bare URL
# ─────────────────────────────────────────────────────────────────────────────


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
        await resolve_rename(
            client, prompt, user.id, 
            url=pending.get("url"), 
            filename=new_name, 
            custom_thumb=pending.get("custom_thumb"),
            media_msg_id=pending.get("media_msg_id")
        )
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
            
        await initiate_rename(message, user.id, orig_filename, url=text)


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
#  Watermark management (Premium only)
# ─────────────────────────────────────────────────────────────────────────────

VALID_WM_POSITIONS = [
    "top-left", "top-center", "top-right",
    "center-left", "center", "center-right",
    "bottom-left", "bottom-center", "bottom-right",
]


@Client.on_message(filters.command("setwatermark") & filters.private)
async def set_watermark_handler(client: Client, message: Message):
    user_id = message.from_user.id
    is_prem = await is_premium_user(user_id)
    if not is_prem and user_id != Config.OWNER_ID and user_id not in Config.ADMIN:
        return await message.reply_text(
            "⭐ **Premium Feature**\n\nWatermarks are for Premium users.", quote=True
        )

    args = message.command
    reply = message.reply_to_message

    # Handle image watermark
    if reply and reply.photo:
        position = "bottom-right"
        if len(args) > 1 and args[-1].lower() in VALID_WM_POSITIONS:
            position = args[-1].lower()
        file_id = reply.photo.file_id
        await set_watermark_image(user_id, file_id, position)
        return await message.reply_text(
            f"✅ **Image Watermark set!**\n📍 **Position:** `{position}`", quote=True
        )

    # Handle text watermark
    if len(args) < 2:
        return await message.reply_text(
            f"❌ Usage: `/setwatermark <text> [position]` or reply to a photo.", quote=True
        )

    parts = args[1:]
    position = "bottom-right"
    if parts[-1].lower() in VALID_WM_POSITIONS:
        position = parts[-1].lower()
        wm_text = " ".join(parts[:-1]).strip()
    else:
        wm_text = " ".join(parts).strip()

    if not wm_text:
        return await message.reply_text("❌ Text cannot be empty.", quote=True)
    if len(wm_text) > 50:
        return await message.reply_text("❌ Text too long (max 50 chars).", quote=True)

    await set_watermark(user_id, wm_text, position)
    await message.reply_text(
        f"✅ **Text Watermark set!**\n📝 **Text:** `{wm_text}`\n📍 **Position:** `{position}`", quote=True
    )


@Client.on_message(filters.command("wmcolor") & filters.private)
async def wmcolor_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not await is_premium_user(user_id):
        return await message.reply_text("⭐ **Premium Only**", quote=True)

    args = message.command
    if len(args) < 2:
        return await message.reply_text("❌ Usage: `/wmcolor #ffffff` or `red`", quote=True)

    color = args[1].lower()
    await update_watermark_field(user_id, "color", color)
    await message.reply_text(f"✅ Watermark color set to `{color}`.", quote=True)


@Client.on_message(filters.command("wmopacity") & filters.private)
async def wmopacity_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not await is_premium_user(user_id):
        return await message.reply_text("⭐ **Premium Only**", quote=True)

    args = message.command
    if len(args) < 2:
        return await message.reply_text("❌ Usage: `/wmopacity <0-100>`", quote=True)

    try:
        opacity = max(0, min(100, int(args[1])))
    except ValueError:
        return await message.reply_text("❌ Opacity must be a number 0-100.", quote=True)

    await update_watermark_field(user_id, "opacity", opacity)
    await message.reply_text(f"✅ Opacity set to `{opacity}%`.", quote=True)


@Client.on_message(filters.command("wmsize") & filters.private)
async def wmsize_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not await is_premium_user(user_id):
        return await message.reply_text("⭐ **Premium Only**", quote=True)

    args = message.command
    if len(args) < 2:
        return await message.reply_text("❌ Usage: `/wmsize <percentage>` (e.g. 10)", quote=True)

    try:
        size = max(1, min(100, int(args[1])))
    except ValueError:
        return await message.reply_text("❌ Size must be a percentage 1-100.", quote=True)

    await update_watermark_field(user_id, "size", size)
    await message.reply_text(f"✅ Size set to `{size}%` of image width/height.", quote=True)


@Client.on_message(filters.command("wmpos") & filters.private)
async def wmpos_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not await is_premium_user(user_id):
        return await message.reply_text("⭐ **Premium Only**", quote=True)

    args = message.command
    if len(args) < 2:
        return await message.reply_text(
            "❌ Usage: `/wmpos <position>`\n\n"
            "Valid positions:\n"
            "`top-left`, `top-center`, `top-right`\n"
            "`center-left`, `center`, `center-right`\n"
            "`bottom-left`, `bottom-center`, `bottom-right`",
            quote=True
        )

    pos = args[1].lower()
    if pos not in VALID_POSITIONS:
        return await message.reply_text("❌ Invalid position. Check `/wmpos` for list.", quote=True)

    await update_watermark_field(user_id, "position", pos)
    await message.reply_text(f"✅ Watermark position set to `{pos}`.", quote=True)


@Client.on_message(filters.command("showwatermark") & filters.private)
async def show_watermark_handler(client: Client, message: Message):
    user_id = message.from_user.id
    wm = await get_watermark(user_id)
    if not wm or (not wm.get("text") and not wm.get("image")):
        return await message.reply_text("❌ No watermark set.", quote=True)
        
    type_str = "🖼️ Image" if wm.get("image") else f"📝 Text (`{wm['text']}`)"
    
    await message.reply_text(
        f"**Your Watermark Settings:**\n\n"
        f"**Type:** {type_str}\n"
        f"📍 **Position:** `{wm.get('position')}`\n"
        f"🎨 **Color:** `{wm.get('color')}`\n"
        f"📏 **Size:** `{wm.get('size')}%`\n"
        f"👁️ **Opacity:** `{wm.get('opacity')}%`\n",
        quote=True,
    )


@Client.on_message(filters.command("clearwatermark") & filters.private)
async def clear_watermark_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await clear_watermark(user_id)
    await message.reply_text("✅ Watermark cleared.", quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Subtitle Commands
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("setsubs") & filters.private)
async def setsubs_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not await is_premium_user(user_id):
        return await message.reply_text("🌟 This is a Premium feature!", quote=True)
    
    args = message.command
    if len(args) < 2:
        return await message.reply_text("Usage: `/setsubs on` or `/setsubs off`", quote=True)
    
    val = args[1].lower()
    if val not in ("on", "off"):
        return await message.reply_text("Usage: `/setsubs on` or `/setsubs off`", quote=True)
    
    from plugins.helper.database import set_subtitle_setting
    await set_subtitle_setting(user_id, "enabled", (val == "on"))
    status = "✅ Enabled" if val == "on" else "❌ Disabled"
    await message.reply_text(f"Subtitle generation is now {status}.", quote=True)


@Client.on_message(filters.command("sublang") & filters.private)
async def sublang_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not await is_premium_user(user_id):
        return await message.reply_text("🌟 This is a Premium feature!", quote=True)
    
    args = message.command
    if len(args) < 2:
        return await message.reply_text("Usage: `/sublang en` (or ja, fr, auto, etc.)", quote=True)
    
    lang = args[1].lower()
    from plugins.helper.database import set_subtitle_setting
    await set_subtitle_setting(user_id, "language", lang)
    await message.reply_text(f"✅ Subtitle language set to: `{lang}`", quote=True)


@Client.on_message(filters.command("submethod") & filters.private)
async def submethod_handler(client: Client, message: Message):
    user_id = message.from_user.id
    await set_subtitle_setting(user_id, "method", method)
    await message.reply_text(f"✅ Subtitle generation method set to: `{method}`", quote=True)


@Client.on_message(filters.command("submodel") & filters.private)
async def submodel_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not await is_premium_user(user_id):
        return await message.reply_text("🌟 This is a Premium feature!", quote=True)
    
    args = message.command
    if len(args) < 2:
        return await message.reply_text(
            "📝 **Subtitle Models**\n\n"
            "● `base` - Fast, low accuracy\n"
            "● `small` - Fast, good accuracy\n"
            "● `distil-large-v3` - **Fast & Ultra Accurate** (Best for 4GB RAM)\n"
            "● `medium` - Slow, high accuracy\n"
            "● `large-v3` - Very slow, professional accuracy\n\n"
            "Usage: `/submodel distil-large-v3`", 
            quote=True
        )
    
    model = args[1].lower()
    if model not in ("base", "small", "distil-large-v3", "medium", "large-v3"):
        return await message.reply_text("❌ Invalid model! Choose `base`, `small`, `distil-large-v3`, `medium`, or `large-v3`.", quote=True)
    
    from plugins.helper.database import set_subtitle_setting
    await set_subtitle_setting(user_id, "model", model)
    await message.reply_text(f"✅ Subtitle model set to: `{model}`", quote=True)


@Client.on_message(filters.command("substats") & filters.private)
async def substats_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not await is_premium_user(user_id):
        return await message.reply_text("🌟 This is a Premium feature!", quote=True)
    
    from plugins.helper.database import get_subtitle_settings
    settings = await get_subtitle_settings(user_id)
    
    status = "✅ Enabled" if settings["enabled"] else "❌ Disabled"
    text = (
        "📝 **Subtitle Settings**\n\n"
        f"● **Status:** {status}\n"
        f"● **Language:** `{settings['language']}`\n"
        f"● **Method:** `{settings['method']}`\n"
        f"● **Engine:** `{settings['engine']}`\n"
        f"● **Local Model:** `{settings['model']}`\n\n"
        "Commands: `/setsubs`, `/sublang`, `/submethod`, `/subengine`, `/submodel`"
    )
    await message.reply_text(text, quote=True)


@Client.on_message(filters.command("subengine") & filters.private)
async def subengine_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if not await is_premium_user(user_id):
        return await message.reply_text("🌟 This is a Premium feature!", quote=True)
    
    args = message.command
    if len(args) < 2:
        return await message.reply_text(
            "⚙️ **Subtitle Alignment Engine**\n\n"
            "● `stable-ts` - **Optimized for CPU/4GB RAM**. Very accurate.\n"
            "● `whisperx` - **Ultra-Accurate alignment**. Professional grade, uses more RAM.\n\n"
            "Usage: `/subengine whisperx`", 
            quote=True
        )
    
    engine = args[1].lower()
    if engine not in ("stable-ts", "whisperx"):
        return await message.reply_text("❌ Invalid engine! Choose `stable-ts` or `whisperx`.", quote=True)
    
    from plugins.helper.database import set_subtitle_setting
    await set_subtitle_setting(user_id, "engine", engine)
    await message.reply_text(f"✅ Subtitle alignment engine set to: `{engine}`", quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Subtitle Callback Handler
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^sub_(srt|burn)\|(.+)"))
async def subtitle_callback_handler(client: Client, query: CallbackQuery):
    action, state_id = query.data.split("|")
    if state_id not in SUBTITLE_STATES:
        return await query.answer("❌ State expired or invalid.", show_alert=True)
    
    state = SUBTITLE_STATES.pop(state_id)
    file_path = state["file_path"]
    srt_path = state["srt_path"]
    user_id = state["user_id"]
    status_msg = await client.get_messages(query.message.chat.id, state["status_msg_id"])
    
    await query.answer()
    
    if action == "sub_srt":
        await status_msg.edit_text("✅ Subtitles sent as document.")
        await client.send_document(
            query.message.chat.id,
            srt_path,
            caption="📝 **AI Generated Subtitles**",
            reply_to_message_id=state["sent_msg_id"]
        )
        # Final cleanup for SRT
        if os.path.exists(srt_path):
            os.remove(srt_path)

    elif action == "sub_burn":
        try:
            from utils.subtitles import burn_subtitles
            
            async def progress_cb(status):
                try:
                    # Could be int (percent) or str (status message)
                    if isinstance(status, int):
                        msg = f"🔥 **Burning Subtitles:** `{status}%`"
                    else:
                        msg = f"🔄 **{status}**"
                    await status_msg.edit_text(f"{msg}\n_(please wait, this take a few minutes)_ ")
                except:
                    pass

            await status_msg.edit_text("🔥 **Burning Subtitles...**")
            burned_video = await burn_subtitles(file_path, srt_path, progress_cb)
            
            if burned_video and os.path.exists(burned_video):
                await status_msg.edit_text("📤 **Uploading processed video...**")
                # Delete the original video so we can reuse name/metadata if needed
                if os.path.exists(file_path):
                    os.remove(file_path)
                
                # Update path to the burned video for upload
                file_path = burned_video
                
                # Upload the final burned video
                await upload_file(
                    client, query.message.chat.id, file_path, "video/mp4",
                    state["caption"], state["thumb_file_id"], status_msg, state["start_time"],
                    user_id=user_id,
                    force_document=state["force_document"],
                    watermark=state["wm_data"]
                )
                await status_msg.edit_text("✅ Subtitles burned & Uploaded!")
            else:
                await status_msg.edit_text("❌ Subtitle burning failed. Sending original video...")
        except Exception as e:
            Config.LOGGER.error(f"Burn error: {e}")
            await status_msg.edit_text(f"❌ Error burning subtitles: {e}")
        finally:
            if os.path.exists(srt_path):
                os.remove(srt_path)
            if os.path.exists(file_path):
                os.remove(file_path)
