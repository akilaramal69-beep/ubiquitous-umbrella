import motor.motor_asyncio
from datetime import datetime, date
from plugins.config import Config

_client = None
_db = None

MAX_DAILY_DOWNLOADS = 50

def get_db():
    global _client, _db
    if _db is None and Config.DATABASE_URL:
        _client = motor.motor_asyncio.AsyncIOMotorClient(Config.DATABASE_URL)
        _db = _client["url_uploader"]
    return _db


async def add_user(user_id: int, username: str | None = None) -> None:
    db = get_db()
    if db is None:
        return
    await db.users.update_one(
        {"_id": user_id},
        {"$setOnInsert": {
            "_id": user_id,
            "username": username,
            "banned": False,
            "caption": "",
            "thumb": None,
            "is_premium": False,
            "download_count": 0,
            "download_date": None,
            "watermark_text": "",
            "watermark_position": "bottom-right",
            "watermark_opacity": 90,
            "watermark_size": 10,
            "watermark_color": "#ffffff",
            "watermark_image": None,
        }},
        upsert=True,
    )


async def get_user(user_id: int) -> dict | None:
    db = get_db()
    if db is None:
        return None
    return await db.users.find_one({"_id": user_id})


async def update_user(user_id: int, data: dict) -> None:
    db = get_db()
    if db is None:
        return
    await db.users.update_one({"_id": user_id}, {"$set": data}, upsert=True)


async def get_all_users() -> list[dict]:
    db = get_db()
    if db is None:
        return []
    return await db.users.find({}).to_list(length=None)


async def total_users_count() -> int:
    db = get_db()
    if db is None:
        return 0
    return await db.users.count_documents({})


async def is_banned(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user and user.get("banned"))


async def ban_user(user_id: int) -> None:
    await update_user(user_id, {"banned": True})


async def unban_user(user_id: int) -> None:
    await update_user(user_id, {"banned": False})


async def is_premium_user(user_id: int) -> bool:
    """Check if user has premium status."""
    from plugins.config import Config
    if user_id in Config.PREMIUM_USERS:
        return True
    user = await get_user(user_id)
    return bool(user and user.get("is_premium", False))


async def set_premium_user(user_id: int, premium: bool) -> None:
    """Set premium status for a user."""
    await update_user(user_id, {"is_premium": premium})


async def get_watermark(user_id: int) -> dict:
    """Get the user's watermark settings.
    Returns a dictionary of all watermark configurations.
    """
    user = await get_user(user_id)
    if not user:
        return {}
        
    return {
        "text": user.get("watermark_text", ""),
        "position": user.get("watermark_position", "bottom-right"),
        "opacity": user.get("watermark_opacity", 90),
        "size": user.get("watermark_size", 10),
        "color": user.get("watermark_color", "#ffffff"),
        "image": user.get("watermark_image", None),
    }


async def set_watermark(user_id: int, text: str, position: str = "bottom-right") -> None:
    """Save watermark text and position for a premium user."""
    await update_user(user_id, {"watermark_text": text, "watermark_position": position, "watermark_image": None})


async def set_watermark_image(user_id: int, file_id: str, position: str = "bottom-right") -> None:
    """Save watermark image and position for a premium user."""
    await update_user(user_id, {"watermark_image": file_id, "watermark_text": "", "watermark_position": position})


async def update_watermark_field(user_id: int, key: str, value: any) -> None:
    """Update a specific watermark field (e.g., color, opacity, size)."""
    await update_user(user_id, {f"watermark_{key}": value})


async def clear_watermark(user_id: int) -> None:
    """Remove watermark settings for a user."""
    await update_user(user_id, {
        "watermark_text": "",
        "watermark_position": "bottom-right",
        "watermark_opacity": 90,
        "watermark_size": 10,
        "watermark_color": "#ffffff",
        "watermark_image": None,
    })


async def check_daily_limit(user_id: int) -> tuple[bool, int]:
    """
    Check if user has reached their daily download limit.
    Returns (can_download, remaining_downloads).
    Premium users have unlimited downloads.
    """
    user = await get_user(user_id)
    if not user:
        return True, MAX_DAILY_DOWNLOADS
    
    if user.get("is_premium", False):
        return True, -1  # Unlimited
    
    today = date.today()
    download_date = user.get("download_date")
    download_count = user.get("download_count", 0)
    
    if download_date is None:
        return True, MAX_DAILY_DOWNLOADS
    
    if isinstance(download_date, datetime):
        download_date = download_date.date()
    elif isinstance(download_date, str):
        try:
            download_date = datetime.fromisoformat(download_date).date()
        except:
            download_date = today
    
    if download_date != today:
        return True, MAX_DAILY_DOWNLOADS
    
    remaining = MAX_DAILY_DOWNLOADS - download_count
    return remaining > 0, remaining


async def increment_download_count(user_id: int) -> None:
    """Increment the user's daily download count."""
    db = get_db()
    if db is None:
        return
        
    user = await get_user(user_id)
    today = date.today()
    today_str = today.isoformat()
    
    if not user:
        await db.users.update_one(
            {"_id": user_id},
            {"$set": {
                "download_count": 1,
                "download_date": today_str
            }},
            upsert=True
        )
        return
    
    download_date = user.get("download_date")
    
    if download_date is None:
        await db.users.update_one(
            {"_id": user_id},
            {"$set": {
                "download_count": 1,
                "download_date": today_str
            }}
        )
        return
    
    if isinstance(download_date, datetime):
        download_date = download_date.date()
    elif isinstance(download_date, str):
        try:
            download_date = datetime.fromisoformat(download_date).date()
        except:
            download_date = today
    
    if download_date != today:
        await db.users.update_one(
            {"_id": user_id},
            {"$set": {
                "download_count": 1,
                "download_date": today_str
            }}
        )
    else:
        current_count = user.get("download_count", 0) + 1
        await db.users.update_one(
            {"_id": user_id},
            {"$set": {"download_count": current_count}}
        )


async def get_user_stats(user_id: int) -> dict:
    """Get user download stats."""
    user = await get_user(user_id)
    if not user:
        return {
            "download_count": 0,
            "download_date": None,
            "is_premium": False,
            "remaining": MAX_DAILY_DOWNLOADS
        }
    
    _, remaining = await check_daily_limit(user_id)
    
    return {
        "download_count": user.get("download_count", 0),
        "download_date": user.get("download_date"),
        "is_premium": user.get("is_premium", False),
        "remaining": remaining
    }
