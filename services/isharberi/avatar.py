"""İş Haberleri botu için profil fotoğrafı (640x640 PNG) üretir."""
import io

from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _font(size):
    try:
        return ImageFont.truetype(FONT_BOLD, size)
    except OSError:
        return ImageFont.load_default()


def make_avatar() -> bytes:
    W = 640
    img = Image.new("RGB", (W, W))
    px = img.load()
    top, bot = (13, 71, 161), (0, 150, 136)  # lacivert → turkuaz
    for y in range(W):
        t = y / (W - 1)
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        for x in range(W):
            px[x, y] = c
    d = ImageDraw.Draw(img)
    # çanta (iş simgesi)
    cx, cy = W // 2, 215
    d.rounded_rectangle([cx - 120, cy - 55, cx + 120, cy + 95], radius=28, fill=(255, 255, 255))
    d.rounded_rectangle([cx - 50, cy - 100, cx + 50, cy - 42], radius=18, outline=(255, 255, 255), width=18)
    d.rectangle([cx - 120, cy + 5, cx + 120, cy + 19], fill=(13, 71, 161))
    d.rounded_rectangle([cx - 21, cy - 6, cx + 21, cy + 30], radius=7, fill=(255, 193, 7))
    # yazılar
    f1, f2 = _font(104), _font(50)
    for text, f, y in (("İŞ", f1, 405), ("HABERLERİ", f2, 500)):
        box = d.textbbox((0, 0), text, font=f)
        d.text(((W - (box[2] - box[0])) // 2, y - (box[3] - box[1]) // 2 - box[1]), text, font=f,
               fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


if __name__ == "__main__":
    open("avatar.png", "wb").write(make_avatar())
