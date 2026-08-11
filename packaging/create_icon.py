"""Generate the IBKR-to-Xero application icon in every format the builds need.

The mark is original artwork: a statement crossing the seam between the two
worlds it travels through. The tile is split diagonally from Interactive
Brokers red (#D81222, the broker side the statement comes from) into Xero blue
(#13B5EA, the ledger side it lands in); the white page carries a red header
rule and grey body rules. No part of either vendor's logo is reproduced.

Every size is drawn at 4x and downsampled, and sizes of 32px and below are
drawn with fewer, heavier details so the page still reads as a page. The
outputs are committed to the repository, so the release builds need neither
this script nor Pillow:

    assets/ibkr-to-xero.png          1024px, for documentation
    assets/ibkr-to-xero.ico          Windows: executables and the installer
    assets/ibkr-to-xero.icns         macOS: the .app bundle
    ibkr_to_xero/resources/app-icon.png  runtime window/dock icon

Regenerate with:  python packaging/create_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "assets"
PACKAGE_RESOURCES = REPO_ROOT / "ibkr_to_xero" / "resources"

SUPERSAMPLE = 4
CORNER_RADIUS = 0.22

IB_RED = (216, 18, 34, 255)
XERO_BLUE = (19, 181, 234, 255)
PAPER = (255, 255, 255, 255)
INK = (60, 68, 80, 255)

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)

# macOS draws app icons without adding padding, so the tile is inset to match
# Apple's template (a 824px tile on a 1024px canvas). Windows icons bleed.
ICNS_MARGIN = 100 / 1024


def _draw_tile(n: int, detailed: bool) -> Image.Image:
    """Draw the tile itself, n x n pixels, already supersampled down."""
    ss = n * SUPERSAMPLE

    def px(fraction: float) -> float:
        return fraction * ss

    tile = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.rounded_rectangle((0, 0, ss - 1, ss - 1), radius=px(CORNER_RADIUS), fill=IB_RED)

    # The Xero half, clipped to the rounded tile.
    mask = Image.new("L", (ss, ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, ss - 1, ss - 1), radius=px(CORNER_RADIUS), fill=255)
    half = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    ImageDraw.Draw(half).polygon([(0, ss), (ss, 0), (ss, ss)], fill=XERO_BLUE)
    tile.alpha_composite(Image.composite(half, Image.new("RGBA", (ss, ss), (0, 0, 0, 0)), mask))

    if detailed:
        page_w, page_h = 0.39, 0.51
        rules = ((0.15, 1.00, IB_RED), (0.40, 0.90, INK), (0.57, 1.00, INK), (0.74, 0.55, INK))
    else:
        page_w, page_h = 0.43, 0.55
        rules = ((0.17, 1.00, IB_RED), (0.45, 0.95, INK), (0.69, 0.60, INK))

    width, height = int(px(page_w)), int(px(page_h))
    page = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    page_draw = ImageDraw.Draw(page)
    page_draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=px(0.04), fill=PAPER)
    pad = width * 0.15
    for top, length, colour in rules:
        y = height * top
        page_draw.rounded_rectangle(
            (pad, y, pad + (width - 2 * pad) * length, y + height * (0.06 if detailed else 0.075)),
            radius=height * 0.03,
            fill=colour,
        )

    page = page.rotate(-6, resample=Image.BICUBIC, expand=True)
    left = int((ss - page.width) / 2)
    top = int((ss - page.height) / 2)

    if detailed:
        shadow = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
        silhouette = Image.new("RGBA", page.size, (0, 0, 0, 255))
        silhouette.putalpha(page.getchannel("A").point(lambda a: a * 110 // 255))
        shadow.alpha_composite(silhouette, (left, int(top + px(0.022))))
        tile.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(px(0.018))))

    tile.alpha_composite(page, (left, top))
    return tile.resize((n, n), Image.LANCZOS)


def render(size: int, margin: float = 0.0) -> Image.Image:
    """Render one icon at `size` pixels, with `margin` of transparent inset."""
    detailed = size > 32
    inset = round(size * margin)
    tile_size = size - 2 * inset
    if inset == 0:
        return _draw_tile(size, detailed)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(_draw_tile(tile_size, detailed), (inset, inset))
    return canvas


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    PACKAGE_RESOURCES.mkdir(exist_ok=True)

    master = render(1024)
    master.save(ASSETS / "ibkr-to-xero.png")
    render(256).save(PACKAGE_RESOURCES / "app-icon.png")

    # Pillow reuses a provided image whenever it matches a requested size, so
    # each entry below is drawn for its own size rather than scaled down.
    ico = [render(size) for size in ICO_SIZES]
    ico[-1].save(ASSETS / "ibkr-to-xero.ico", sizes=list(zip(ICO_SIZES, ICO_SIZES)), append_images=ico[:-1])

    icns = [render(size, margin=ICNS_MARGIN) for size in ICNS_SIZES]
    icns[-1].save(ASSETS / "ibkr-to-xero.icns", append_images=icns[:-1])

    print(f"wrote {ASSETS}/ibkr-to-xero.{{png,ico,icns}} and {PACKAGE_RESOURCES}/app-icon.png")


if __name__ == "__main__":
    main()
