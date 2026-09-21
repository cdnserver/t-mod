"""Create deterministic NSIS artwork from the approved Blackbird mark."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parent
MASTER = ROOT.parents[1] / "src" / "renderer" / "assets" / "blackbird" / "master.png"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def gradient(size: tuple[int, int]) -> Image.Image:
    width, height = size
    canvas = Image.new("RGB", size, "#080a0b")
    pixels = canvas.load()
    for y in range(height):
        for x in range(width):
            glow = max(0.0, 1.0 - (((x - width * .72) / width) ** 2 + ((y - height * .22) / height) ** 2) ** .5 * 2.4)
            base = 8 + int(glow * 13)
            pixels[x, y] = (base - 1, base + 2, base + 1)
    return canvas


def draw_grid(canvas: Image.Image, step: int) -> None:
    draw = ImageDraw.Draw(canvas)
    for x in range(0, canvas.width, step):
        draw.line((x, 0, x, canvas.height), fill=(15, 17, 18), width=1)
    for y in range(0, canvas.height, step):
        draw.line((0, y, canvas.width, y), fill=(15, 17, 18), width=1)


def paste_mark(canvas: Image.Image, box: tuple[int, int, int, int], opacity: float = 1.0) -> None:
    mark = Image.open(MASTER).convert("RGBA")
    mark.thumbnail((box[2], box[3]), Image.Resampling.LANCZOS)
    if opacity < 1:
        mark.putalpha(mark.getchannel("A").point(lambda value: int(value * opacity)))
    canvas.paste(mark, (box[0], box[1]), mark)


def sidebar() -> Image.Image:
    canvas = gradient((164, 314))
    draw_grid(canvas, 41)
    glow = Image.new("RGBA", canvas.size)
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-44, 3, 208, 255), fill=(142, 225, 199, 28))
    glow = glow.filter(ImageFilter.GaussianBlur(37))
    canvas.paste(glow.convert("RGB"), (0, 0), glow.getchannel("A"))
    paste_mark(canvas, (7, 32, 150, 150), .92)
    draw = ImageDraw.Draw(canvas)
    draw.line((19, 211, 145, 211), fill=(119, 137, 130), width=1)
    draw.text((19, 229), "BLACKBIRD", font=font(15, True), fill="#F1F5F3")
    draw.text((19, 251), "PRIVATE SYSTEM", font=font(8, True), fill="#8FA098")
    draw.text((19, 288), "TECHNOLOGIES", font=font(7, True), fill="#627069")
    return canvas


def header() -> Image.Image:
    canvas = gradient((150, 57))
    draw_grid(canvas, 29)
    paste_mark(canvas, (99, 2, 53, 53), .95)
    draw = ImageDraw.Draw(canvas)
    draw.text((10, 13), "BLACKBIRD", font=font(12, True), fill="#F3F6F5")
    draw.text((10, 33), "SECURE INSTALL", font=font(7, True), fill="#84948C")
    return canvas


if __name__ == "__main__":
    sidebar().save(ROOT / "installer-sidebar.bmp", format="BMP")
    header().save(ROOT / "installer-header.bmp", format="BMP")
