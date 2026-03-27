import asyncio
import functools
from pyrogram import Client, filters
from pyrogram.types import Message
from plugins.config import Config
from plugins.helper.database import (
    get_all_users, total_users_count, ban_user, unban_user, is_banned,
    is_premium_user, set_premium_user, get_user_stats
)
from plugins.helper.upload import humanbytes
import psutil
import os


def admin_only(func):
    """Decorator: only owner or admins can run this."""
    @functools.wraps(func)
    async def wrapper(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id != Config.OWNER_ID and user_id not in Config.ADMIN:
            return await message.reply_text("🚫 Admin only command.", quote=True)
        return await func(client, message)
    return wrapper


# ── /total ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("total") & filters.private)
@admin_only
async def total_users(client: Client, message: Message):
    count = await total_users_count()
    await message.reply_text(f"👥 **Total registered users:** `{count}`", quote=True)


# ── /statusall (admin status) ────────────────────────────────────────────────

@Client.on_message(filters.command("statusall") & filters.private)
@admin_only
async def statusall_handler(client: Client, message: Message):
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("./")
    count = await total_users_count()

    from plugins.helper.upload import check_ffmpeg
    ffmpeg_found = await check_ffmpeg()

    text = (
        "🚀 **Bot Status**\n\n"
        f"🖥 **CPU:** {cpu}%\n"
        f"🧠 **RAM:** {humanbytes(ram.used)} / {humanbytes(ram.total)} ({ram.percent}%)\n"
        f"💽 **Disk:** {humanbytes(disk.used)} / {humanbytes(disk.total)} ({disk.percent}%)\n"
        f"👥 **Users:** {count}\n"
        f"🎥 **FFmpeg:** {'✅ Found' if ffmpeg_found else '❌ Not Found'}"
    )
    await message.reply_text(text, quote=True)


# ── /broadcast ────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("broadcast") & filters.private)
@admin_only
async def broadcast_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2 and not message.reply_to_message:
        return await message.reply_text("Usage: `/broadcast <message>` or reply to a message with /broadcast", quote=True)

    broadcast_text = (
        " ".join(args[1:]) if len(args) > 1
        else message.reply_to_message.text or message.reply_to_message.caption or ""
    )
    if not broadcast_text:
        return await message.reply_text("❌ Nothing to broadcast.", quote=True)

    users = await get_all_users()
    sent, failed = 0, 0
    status = await message.reply_text(f"📢 Broadcasting to **{len(users)}** users…", quote=True)

    for user in users:
        try:
            await client.send_message(user["_id"], broadcast_text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ **Broadcast complete!**\n\n✔️ Sent: `{sent}`\n❌ Failed: `{failed}`"
    )


# ── /ban ──────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("ban") & filters.private)
@admin_only
async def ban_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply_text("Usage: `/ban <user_id>`", quote=True)
    try:
        target = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid user ID.", quote=True)
    await ban_user(target)
    await message.reply_text(f"⛔ User `{target}` has been banned.", quote=True)


# ── /unban ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("unban") & filters.private)
@admin_only
async def unban_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply_text("Usage: `/unban <user_id>`", quote=True)
    try:
        target = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid user ID.", quote=True)
    await unban_user(target)
    await message.reply_text(f"✅ User `{target}` has been unbanned.", quote=True)


# ── /premium ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("premium") & filters.private)
@admin_only
async def premium_handler(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply_text(
            "Usage:\n"
            "`/premium <user_id>` - Check premium status\n"
            "`/premium <user_id> on` - Enable premium\n"
            "`/premium <user_id> off` - Disable premium",
            quote=True
        )
    
    try:
        target = int(args[1])
    except ValueError:
        return await message.reply_text("❌ Invalid user ID.", quote=True)
    
    if len(args) == 2:
        is_prem = await is_premium_user(target)
        status = "⭐ **Premium**" if is_prem else "👤 **Free**"
        await message.reply_text(f"User `{target}` status: {status}", quote=True)
    elif args[2].lower() == "on":
        await add_user(target)  # Ensure user document is initialized to prevent bugs
        await set_premium_user(target, True)
        await message.reply_text(f"✅ User `{target}` is now **⭐ Premium**!", quote=True)
    elif args[2].lower() == "off":
        await add_user(target)
        await set_premium_user(target, False)
        await message.reply_text(f"✅ User `{target}` is now **👤 Free**.", quote=True)
    else:
        await message.reply_text("Usage: `/premium <user_id> on/off`", quote=True)
