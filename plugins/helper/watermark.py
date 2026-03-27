"""
Watermark utility using Pillow.
Premium feature: Add customizable watermarks to thumbnails and videos.
"""
import os
import io
import random
from PIL import Image, ImageDraw, ImageFont
from plugins.config import Config

WATERMARK_POSITIONS = {
    "top-left": lambda w, h, tw, th: (10, 10),
    "top-right": lambda w, h, tw, th: (w - tw - 10, 10),
    "bottom-left": lambda w, h, tw, th: (10, h - th - 10),
    "bottom-right": lambda w, h, tw, th: (w - tw - 10, h - th - 10),
    "center": lambda w, h, tw, th: ((w - tw) // 2, (h - th) // 2),
    "top-center": lambda w, h, tw, th: ((w - tw) // 2, 10),
    "bottom-center": lambda w, h, tw, th: ((w - tw) // 2, h - th - 10),
}

DEFAULT_SETTINGS = {
    "enabled": False,
    "text": "PREMIUM",
    "position": "bottom-right",
    "font_size": 24,
    "color": (255, 255, 255, 200),
    "outline_color": (0, 0, 0, 255),
    "outline_width": 2,
    "opacity": 1.0,
    "shadow": True,
    "angle": 0,
}

FONT_CACHE = {}

def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """Get font, cached for performance."""
    if size not in FONT_CACHE:
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        ]
        
        for font_path in font_paths:
            if os.path.exists(font_path):
                try:
                    FONT_CACHE[size] = ImageFont.truetype(font_path, size)
                    return FONT_CACHE[size]
                except Exception:
                    pass
        
        FONT_CACHE[size] = ImageFont.load_default()
    
    return FONT_CACHE[size]


def create_watermark_image(
    text: str,
    font_size: int = 24,
    color: tuple = (255, 255, 255, 200),
    outline_color: tuple = (0, 0, 0, 255),
    outline_width: int = 2,
    angle: float = 0,
) -> Image.Image:
    """Create a watermark image with the given text."""
    font = _get_font(font_size)
    
    bbox = font.getbbox(text)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    padding = 20
    img_width = text_width + padding * 2 + outline_width * 2
    img_height = text_height + padding * 2 + outline_width * 2
    
    img = Image.new("RGBA", (img_width, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    x = padding + outline_width
    y = padding + outline_width
    
    if outline_width > 0:
        for ox in range(-outline_width, outline_width + 1):
            for oy in range(-outline_width, outline_width + 1):
                if ox * ox + oy * oy <= outline_width * outline_width:
                    draw.text((x + ox, y + oy), text, font=font, fill=outline_color)
    
    draw.text((x, y), text, font=font, fill=color)
    
    if angle != 0:
        from PIL import ImageOps
        img = img.rotate(angle, expand=True, fillcolor=(0, 0, 0, 0))
    
    return img


def add_text_watermark(
    image_path: str,
    output_path: str,
    settings: dict,
) -> bool:
    """
    Add text watermark to an image.
    
    Args:
        image_path: Path to input image
        output_path: Path to save watermarked image
        settings: Watermark settings dict
    
    Returns:
        True if successful, False otherwise
    """
    try:
        img = Image.open(image_path).convert("RGBA")
        w, h = img.size
        
        text = settings.get("text", DEFAULT_SETTINGS["text"])
        position_key = settings.get("position", DEFAULT_SETTINGS["position"])
        font_size = settings.get("font_size", DEFAULT_SETTINGS["font_size"])
        color = tuple(settings.get("color", DEFAULT_SETTINGS["color"]))
        outline_color = tuple(settings.get("outline_color", DEFAULT_SETTINGS["outline_color"]))
        outline_width = settings.get("outline_width", DEFAULT_SETTINGS["outline_width"])
        angle = settings.get("angle", DEFAULT_SETTINGS["angle"])
        shadow = settings.get("shadow", DEFAULT_SETTINGS["shadow"])
        opacity = settings.get("opacity", DEFAULT_SETTINGS["opacity"])
        
        wm_img = create_watermark_image(
            text=text,
            font_size=font_size,
            color=color,
            outline_color=outline_color,
            outline_width=outline_width,
            angle=angle,
        )
        
        tw, th = wm_img.size
        
        if shadow:
            shadow_img = wm_img.copy()
            shadow_img_array = Image.new("RGBA", (tw + 4, th + 4), (0, 0, 0, 0))
            shadow_draw = ImageDraw.Draw(shadow_img_array)
            for sx in range(-2, 3):
                for sy in range(-2, 3):
                    shadow_draw.text((2 + sx, 2 + sy), text, font=_get_font(font_size), fill=(0, 0, 0, 100))
            shadow_img_array.paste(wm_img, (0, 0), wm_img)
            wm_img = shadow_img_array
        
        if opacity < 1.0:
            alpha = wm_img.split()[3]
            alpha = alpha.point(lambda p: int(p * opacity))
            wm_img.putalpha(alpha)
        
        pos_func = WATERMARK_POSITIONS.get(position_key, WATERMARK_POSITIONS["bottom-right"])
        x, y = pos_func(w, h, tw, th)
        
        if x + tw > w:
            x = w - tw
        if y + th > h:
            y = h - th
        if x < 0:
            x = 0
        if y < 0:
            y = 0
        
        watermarked = Image.new("RGBA", img.size, (0, 0, 0, 0))
        watermarked.paste(img, (0, 0))
        watermarked.paste(wm_img, (x, y), wm_img)
        
        final = watermarked.convert("RGB")
        final.save(output_path, "JPEG", quality=95)
        
        return True
        
    except Exception as e:
        Config.LOGGER.error(f"Watermark error: {e}")
        return False


def add_image_watermark(
    image_path: str,
    output_path: str,
    watermark_path: str,
    position: str = "bottom-right",
    opacity: float = 1.0,
    scale: float = 0.2,
) -> bool:
    """
    Add image watermark to an image.
    
    Args:
        image_path: Path to input image
        output_path: Path to save watermarked image
        watermark_path: Path to watermark image (PNG with transparency)
        position: Position preset
        opacity: Opacity 0.0-1.0
        scale: Scale relative to image size
    
    Returns:
        True if successful, False otherwise
    """
    try:
        img = Image.open(image_path).convert("RGBA")
        wm_img = Image.open(watermark_path).convert("RGBA")
        
        iw, ih = img.size
        
        new_w = int(iw * scale)
        new_h = int(new_w * wm_img.size[1] / wm_img.size[0])
        wm_img = wm_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        
        if opacity < 1.0:
            alpha = wm_img.split()[3]
            alpha = alpha.point(lambda p: int(p * opacity))
            wm_img.putalpha(alpha)
        
        tw, th = wm_img.size
        
        pos_func = WATERMARK_POSITIONS.get(position, WATERMARK_POSITIONS["bottom-right"])
        x, y = pos_func(iw, ih, tw, th)
        
        if x + tw > iw:
            x = iw - tw
        if y + th > ih:
            y = ih - th
        
        watermarked = Image.new("RGBA", img.size, (0, 0, 0, 0))
        watermarked.paste(img, (0, 0))
        watermarked.paste(wm_img, (x, y), wm_img)
        
        final = watermarked.convert("RGB")
        final.save(output_path, "JPEG", quality=95)
        
        return True
        
    except Exception as e:
        Config.LOGGER.error(f"Image watermark error: {e}")
        return False


def generate_preview(
    settings: dict,
    width: int = 400,
    height: int = 225,
) -> bytes:
    """Generate a preview image showing watermark placement."""
    try:
        img = Image.new("RGB", (width, height), (
            random.randint(20, 60),
            random.randint(40, 80),
            random.randint(100, 160)
        ))
        
        bg = Image.new("RGB", (width, height))
        for x in range(width):
            for y in range(height):
                noise = random.randint(-15, 15)
                r = max(0, min(255, img.getpixel((x, y))[0] + noise))
                g = max(0, min(255, img.getpixel((x, y))[1] + noise))
                b = max(0, min(255, img.getpixel((x, y))[2] + noise))
                bg.putpixel((x, y), (r, g, b))
        
        img = bg
        
        text = settings.get("text", DEFAULT_SETTINGS["text"])
        position_key = settings.get("position", DEFAULT_SETTINGS["position"])
        font_size = settings.get("font_size", DEFAULT_SETTINGS["font_size"])
        color = tuple(settings.get("color", DEFAULT_SETTINGS["color"]))
        outline_color = tuple(settings.get("outline_color", DEFAULT_SETTINGS["outline_color"]))
        outline_width = settings.get("outline_width", DEFAULT_SETTINGS["outline_width"])
        angle = settings.get("angle", DEFAULT_SETTINGS["angle"])
        
        wm_img = create_watermark_image(
            text=text,
            font_size=font_size,
            color=color,
            outline_color=outline_color,
            outline_width=outline_width,
            angle=angle,
        )
        
        tw, th = wm_img.size
        
        pos_func = WATERMARK_POSITIONS.get(position_key, WATERMARK_POSITIONS["bottom-right"])
        x, y = pos_func(width, height, tw, th)
        
        img.paste(wm_img, (x, y), wm_img)
        
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        
        return buf.getvalue()
        
    except Exception as e:
        Config.LOGGER.error(f"Preview generation error: {e}")
        return b""


def validate_settings(settings: dict) -> tuple[bool, str]:
    """Validate watermark settings."""
    if not isinstance(settings, dict):
        return False, "Settings must be a dictionary"
    
    text = settings.get("text", "")
    if not text or len(text) > 50:
        return False, "Text must be 1-50 characters"
    
    position = settings.get("position", "")
    if position not in WATERMARK_POSITIONS:
        return False, f"Invalid position. Choose: {', '.join(WATERMARK_POSITIONS.keys())}"
    
    font_size = settings.get("font_size", 24)
    if not isinstance(font_size, int) or font_size < 8 or font_size > 200:
        return False, "Font size must be 8-200"
    
    opacity = settings.get("opacity", 1.0)
    if not isinstance(opacity, (int, float)) or opacity < 0.1 or opacity > 1.0:
        return False, "Opacity must be 0.1-1.0"
    
    angle = settings.get("angle", 0)
    if not isinstance(angle, (int, float)) or angle < -180 or angle > 180:
        return False, "Angle must be -180 to 180"
    
    return True, "Valid"
