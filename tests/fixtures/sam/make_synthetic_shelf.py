"""Regenerate tests/fixtures/shelf_12_synthetic.mp4: a 10 s pan across 12 labelled boxes.

    uv run --with pillow python tests/fixtures/sam/make_synthetic_shelf.py shelf.png
    ffmpeg -loop 1 -i shelf.png -t 10 -r 30 \\
        -vf "crop=1280:720:'(iw-1280)*t/10':0,format=yuv420p" -c:v libx264 \\
        -movflags +faststart tests/fixtures/shelf_12_synthetic.mp4

The recorded SAM 3.1 responses next to this file are for that clip at 5 fps.
"""

import random
import sys

from PIL import Image, ImageDraw, ImageFont

BOXES = 12


def main(out: str) -> None:
    random.seed(3)
    im = Image.new("RGB", (3200, 720), (182, 186, 190))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 560, 3200, 600], fill=(90, 90, 95))  # shelf plank
    try:
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34
        )
    except OSError:
        font = ImageFont.load_default()
    x = 120
    for _ in range(BOXES):
        w, h = random.randint(170, 210), random.randint(230, 300)
        colour = (150 + random.randint(-15, 15), 105 + random.randint(-10, 10), 60)
        d.rectangle([x, 560 - h, x + w, 560], fill=colour, outline=(80, 55, 30), width=4)
        d.line([x, 560 - h + 30, x + w, 560 - h + 30], fill=(120, 85, 45), width=3)
        d.rectangle([x + 20, 560 - h + 70, x + w - 20, 560 - h + 150], fill=(245, 245, 240))
        d.text((x + 30, 560 - h + 90), "6205", fill=(20, 20, 20), font=font)
        x += w + 60
    im.save(out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "shelf.png")
