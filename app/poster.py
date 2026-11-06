"""Draws the notice poster (2:3) and background art (16:9) with Pillow."""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import config, storage

FONTS = Path(__file__).parent / "fonts"
HEAD_FONT = FONTS / "Anton-Regular.ttf"
BOLD_FONT = FONTS / "Poppins-Bold.ttf"
BODY_FONT = FONTS / "Poppins-Medium.ttf"

POSTER_SIZE = (1000, 1500)
ART_SIZE = (1920, 1080)


def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _font(path, size):
    return ImageFont.truetype(str(path), size)


def _gradient(size, top, bottom):
    w, h = size
    top, bottom = _hex(top), _hex(bottom)
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return grad.resize(size)


def _cover(img: Image.Image, size):
    return ImageOps.fit(img.convert("RGB"), size, Image.LANCZOS, centering=(0.5, 0.5))


def _load_image(image_id: str):
    meta = config.find_image(image_id) if image_id else None
    if not meta:
        return None
    path = storage.IMAGES_DIR / meta["filename"]
    if not path.exists():
        return None
    try:
        img = Image.open(path)
        img.load()
        return img
    except Exception:
        return None


def _background(size, theme, bg_image_id):
    base = _gradient(size, theme["top"], theme["bottom"])
    img = _load_image(bg_image_id)
    if img is None:
        return base
    photo = _cover(img, size)
    # Darken the photo and tint it with the theme so text stays readable.
    shade = Image.new("RGB", size, _hex(theme["bottom"]))
    photo = Image.blend(photo, shade, 0.62)
    return Image.blend(photo, base, 0.25)


def _stripes(draw, box, accent, dark=(14, 14, 16), width=46):
    x0, y0, x1, y1 = box
    h = y1 - y0
    draw.rectangle(box, fill=dark)
    x = x0 - h
    i = 0
    while x < x1 + h:
        if i % 2 == 0:
            draw.polygon([(x, y1), (x + h, y0), (x + h + width, y0), (x + width, y1)], fill=accent)
        x += width
        i += 1


def _warning_icon(draw, cx, top, size, accent, dark):
    h = int(size * 0.87)
    pts = [(cx, top), (cx + size // 2, top + h), (cx - size // 2, top + h)]
    draw.polygon(pts, fill=accent)
    # rounded-ish look: draw a slightly smaller inner triangle edge
    bar_w = max(6, size // 9)
    bar_top = top + int(h * 0.34)
    bar_bot = top + int(h * 0.68)
    draw.rounded_rectangle((cx - bar_w // 2, bar_top, cx + bar_w // 2, bar_bot), radius=bar_w // 2, fill=dark)
    dot = int(bar_w * 0.75)
    dy = top + int(h * 0.80)
    draw.ellipse((cx - dot, dy - dot, cx + dot, dy + dot), fill=dark)
    return top + h


def _wrap(text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit(text, font_path, max_w, max_h, start, minimum, max_lines, line_factor):
    """Largest font size where the wrapped text fits the box."""
    size = start
    while size >= minimum:
        f = _font(font_path, size)
        lines = _wrap(text, f, max_w)
        too_wide = any(f.getlength(l) > max_w for l in lines)
        total_h = size * line_factor * len(lines)
        if not too_wide and len(lines) <= max_lines and total_h <= max_h:
            return f, lines, size
        size -= 4
    f = _font(font_path, minimum)
    return f, _wrap(text, f, max_w), minimum


def _cap_height(font):
    return -font.getbbox("H", anchor="ls")[1]


def _block_height(lines, font, size, line_factor):
    return _cap_height(font) + int(size * line_factor) * (len(lines) - 1)


def _draw_lines(draw, lines, font, x, y, size, line_factor, fill, align="m"):
    """Draw lines with the top of the capitals at y. Returns the y below the last line."""
    lh = int(size * line_factor)
    base = y + _cap_height(font)
    for i, line in enumerate(lines):
        draw.text((x, base + i * lh), line, font=font, fill=fill, anchor=f"{align}s")
    descent = font.getbbox("g", anchor="ls")[3]
    return base + lh * (len(lines) - 1) + descent


def _draw_kicker(draw, text, x, y, size, fill, align="center"):
    f = _font(BOLD_FONT, size)
    track = size * 0.18
    widths = [f.getlength(ch) for ch in text]
    total = sum(widths) + track * (len(text) - 1)
    cx = x - total / 2 if align == "center" else x
    for ch, w in zip(text, widths):
        draw.text((cx, y), ch, font=f, fill=fill, align="l")
        cx += w + track
    return y + int(size * 1.4)


def _to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def render(preset: dict, kind: str = "poster") -> bytes:
    """Return JPEG bytes. kind = 'poster' (2:3) or 'art' (16:9)."""
    size = POSTER_SIZE if kind == "poster" else ART_SIZE

    # Uploaded full-image mode: just crop the image to shape.
    if preset.get("poster_mode") == "image":
        img = _load_image(preset.get("image_id", ""))
        if img is not None:
            out = _cover(img, size)
            if kind == "art":
                shade = Image.new("RGB", size, (8, 8, 10))
                out = Image.blend(out, shade, 0.35)
            return _to_bytes(out)
        # fall through to generated if the image is missing

    theme = config.THEMES.get(preset.get("theme") or "amber", config.THEMES["amber"])
    accent = _hex(theme["accent"])
    dark = (14, 14, 16)
    white = (246, 246, 246)
    muted = (190, 194, 202)

    img = _background(size, theme, preset.get("bg_image_id", ""))
    draw = ImageDraw.Draw(img)
    w, h = size

    headline = (preset.get("headline") or "SERVER NOTICE").upper()
    kicker = (preset.get("kicker") or "").upper()
    subline = preset.get("subline") or ""
    footer = preset.get("footer") or ""
    stripes = bool(preset.get("stripes", True))

    band = int(h * 0.045) if stripes else 0
    if stripes:
        _stripes(draw, (0, 0, w, band), accent, dark)
        _stripes(draw, (0, h - band, w, h), accent, dark)

    if kind == "poster":
        pad = 70
        y = band + 110
        y = _warning_icon(draw, w // 2, y, 210, accent, dark) + 50
        if kicker:
            y = _draw_kicker(draw, kicker, w // 2, y, 40, accent) + 10
        # reserve room for subline/footer below
        bottom_reserved = band + 60 + (150 if subline else 0) + (70 if footer else 0)
        box_h = h - y - bottom_reserved
        f, lines, fs = _fit(headline, HEAD_FONT, w - pad * 2, box_h, 230, 60, 4, 1.04)
        block_h = _block_height(lines, f, fs, 1.04)
        y = y + max(0, (box_h - block_h) // 2)
        y = _draw_lines(draw, lines, f, w // 2, y, fs, 1.04, white)
        if subline:
            y += 30
            draw.rectangle((w // 2 - 60, y, w // 2 + 60, y + 6), fill=accent)
            y += 36
            sf, slines, ss = _fit(subline, BODY_FONT, w - pad * 2, 130, 52, 28, 2, 1.25)
            _draw_lines(draw, slines, sf, w // 2, y, ss, 1.25, white)
        if footer:
            ff, flines, fsz = _fit(footer, BODY_FONT, w - pad * 2, 50, 34, 22, 1, 1.2)
            draw.text((w // 2, h - band - 40), flines[0], font=ff, fill=muted, anchor="ms")
    else:
        pad = 140
        left = pad
        max_w = int(w * 0.62)
        y = band + 120
        icon_size = 150
        _warning_icon(draw, left + icon_size // 2, y, icon_size, accent, dark)
        if kicker:
            _draw_kicker(draw, kicker, left + icon_size + 40, y + int(icon_size * 0.87) // 2 - 26, 44, accent, align="left")
        y += int(icon_size * 0.87) + 50
        bottom_reserved = band + 80 + (120 if subline else 0) + (60 if footer else 0)
        box_h = h - y - bottom_reserved
        f, lines, fs = _fit(headline, HEAD_FONT, max_w, box_h, 200, 60, 3, 1.04)
        y = _draw_lines(draw, lines, f, left, y, fs, 1.04, white, align="l")
        if subline:
            y += 24
            draw.rectangle((left, y, left + 120, y + 6), fill=accent)
            y += 32
            sf, slines, ss = _fit(subline, BODY_FONT, max_w, 110, 50, 26, 2, 1.25)
            _draw_lines(draw, slines, sf, left, y, ss, 1.25, white, align="l")
        if footer:
            ff, flines, fsz = _fit(footer, BODY_FONT, max_w, 50, 34, 22, 1, 1.2)
            draw.text((left, h - band - 50), flines[0], font=ff, fill=muted, anchor="ls")

    return _to_bytes(img)
