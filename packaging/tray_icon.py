"""Programmatic tray icon for MLAC Studio."""

from __future__ import annotations

from PIL import Image, ImageDraw


def create_tray_image(size: int = 64) -> Image.Image:
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def box(values: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        return tuple(round(value * scale) for value in values)

    draw.rounded_rectangle(box((3, 3, 61, 61)), radius=round(14 * scale), fill=(13, 45, 67, 255))
    draw.ellipse(box((14, 13, 49, 48)), outline=(245, 190, 66, 255), width=max(2, round(6 * scale)))
    draw.line(box((40, 41, 53, 54)), fill=(245, 190, 66, 255), width=max(2, round(6 * scale)))
    draw.ellipse(box((25, 24, 35, 34)), fill=(238, 245, 239, 255))
    return image
