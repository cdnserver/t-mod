"""Build deterministic Blackbird raster exports from the approved masters."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parent
SOURCES = {
    "blackbird": ROOT / "blackbird-master.png",
    **{
        name: ROOT / "services" / f"{name}.png"
        for name in ("reactor", "consensus", "atlas", "sgl", "ovr", "games", "tasks", "admin")
    },
}
SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
VARIANTS = {
    "fullcolor": None,
    "white": "#FFFFFF",
    "black": "#05070A",
    "ice": "#BCEBFF",
}


def resized(image: Image.Image, size: int) -> Image.Image:
    return image.resize((size, size), Image.Resampling.LANCZOS)


def recolor(image: Image.Image, color: str) -> Image.Image:
    alpha = image.getchannel("A")
    result = Image.new("RGBA", image.size, color)
    result.putalpha(alpha)
    return result


def export() -> None:
    output = ROOT / "exports"
    for name, source in SOURCES.items():
        image = Image.open(source).convert("RGBA")
        for variant, color in VARIANTS.items():
            variant_image = image if color is None else recolor(image, color)
            destination = output / variant / name
            destination.mkdir(parents=True, exist_ok=True)
            for size in SIZES:
                resized(variant_image, size).save(
                    destination / f"{name}-{size}.png",
                    optimize=True,
                )

    master = Image.open(SOURCES["blackbird"]).convert("RGBA")
    favicon = output / "favicon"
    favicon.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 180, 192, 512):
        resized(master, size).save(favicon / f"blackbird-{size}.png", optimize=True)
    resized(master, 512).save(
        favicon / "blackbird.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    export()
