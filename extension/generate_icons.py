"""
KAM TTS icon generator — high quality via supersampling.

Why supersampling: drawing shapes/text directly at 16/48/128px produces jagged,
pixelated edges because PIL's primitives are not antialiased at the target size.
Instead we render each icon at SS× the target resolution, then downscale with the
LANCZOS resampling filter. The downscale averages many source pixels into each
final pixel, yielding smooth antialiased curves and crisp text — roughly the
"3× clearer" the user asked for (we use 8× headroom for the smallest sizes).

Design: black disc, white ring, "KAM" wordmark in black with a yellow outline.
"""
from PIL import Image, ImageDraw, ImageFont
import os

# Pillow 9.1+ moved resampling filters to Image.Resampling; the old top-level
# Image.LANCZOS still works at runtime but Pylance's stubs only know the new
# path. Resolve once here so the code is clean on both old and new Pillow.
try:
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    _LANCZOS = Image.LANCZOS  # type: ignore[attr-defined]

YELLOW = (245, 197, 24, 255)   # #f5c518
BLACK  = (0, 0, 0, 255)
WHITE  = (255, 255, 255, 255)

# Candidate bold fonts, in preference order. Liberation Sans Bold is metrically
# compatible with Arial; DejaVu Sans Bold is the universal fallback.
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "arialbd.ttf",
    "arial.ttf",
]

OUT_DIR = os.environ.get("KAM_ICON_OUT", ".")
SS = 8   # supersampling factor — render at SS× then downscale


def _load_font(px):
    """Load a TrueType font at the requested size, falling back to the
    default bitmap font so icon generation never hard-fails on a bare system."""
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, px)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def render_icon(target):
    """Render one KAM icon at the given pixel size. Drawn supersampled and
    downsampled with LANCZOS so edges stay clean at 16px."""
    big = target * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    inset = max(1, big // 64)

    # ONE design at every size: black disc + white ring + yellow "KAM".
    # At small sizes the ring is made thinner and tighter and the wordmark is
    # grown (and condensed) to fill the inner circle, so the same look that
    # reads well at 128 also reads at 48 and 16 instead of shrinking to a speck.
    if target >= 96:        # 128 — the reference look
        ring_pad   = big // 13
        ring_w     = max(2, big // 30)
        text_avail = 0.62          # KAM stays inside the ring
        text_ratio = 0.27
        condensed  = False
    elif target >= 32:      # 48 — thinner ring, larger KAM
        ring_pad   = big // 22
        ring_w     = max(3, big // 22)
        text_avail = 0.78
        text_ratio = 0.5
        condensed  = True
    else:                   # 16 — thinnest ring, KAM fills the inner disc
        ring_pad   = big // 30
        ring_w     = max(3, big // 18)
        text_avail = 0.86
        text_ratio = 0.6
        condensed  = True

    # Black disc.
    draw.ellipse([inset, inset, big-1-inset, big-1-inset], fill=BLACK)
    # White ring.
    draw.ellipse([ring_pad, ring_pad, big-1-ring_pad, big-1-ring_pad],
                 outline=WHITE, width=ring_w)
    # Yellow "KAM", sized to fill the available inner width for this size.
    _draw_text(draw, big, "KAM", fill=YELLOW, outline=None,
               avail=text_avail, ratio=text_ratio, hcap=0.9, condensed=condensed)

    return img.resize((target, target), _LANCZOS)


def _load_condensed(px):
    """Load the condensed face used for the wordmark, with graceful
    fallback when the font is unavailable."""
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Bold.ttf"):
        try:
            return ImageFont.truetype(path, px)
        except (OSError, IOError):
            continue
    return _load_font(px)


def _draw_text(draw, big, text, fill, outline, avail, ratio, hcap=0.74, condensed=False):
    """Fit `text` to `avail` fraction of width via binary search, centre it."""
    loader = _load_condensed if condensed else _load_font
    lo, hi = 4, int(big * 1.4)   # allow very large fonts for full-tile fill
    best = loader(int(big * ratio))
    while lo <= hi:
        mid = (lo + hi) // 2
        f = loader(mid)
        bb = draw.textbbox((0, 0), text, font=f)
        if (bb[2]-bb[0]) <= big*avail and (bb[3]-bb[1]) <= big*hcap:
            best = f; lo = mid + 1
        else:
            hi = mid - 1
    f = best
    bb = draw.textbbox((0, 0), text, font=f)
    x = (big - (bb[2]-bb[0])) // 2 - bb[0]
    y = (big - (bb[3]-bb[1])) // 2 - bb[1]
    if outline:
        o = max(2, big // 80)
        for dx in (-o, 0, o):
            for dy in (-o, 0, o):
                if dx or dy:
                    draw.text((x+dx, y+dy), text, fill=outline, font=f)
    draw.text((x, y), text, fill=fill, font=f)


def main():
    """Regenerate every icon size the extension manifest references."""
    os.makedirs(OUT_DIR, exist_ok=True)
    for size in (16, 48, 128):
        icon = render_icon(size)
        path = os.path.join(OUT_DIR, f"icon{size}.png")
        icon.save(path)
        print(f"Created {path} ({size}x{size}, supersampled {SS}x)")
    print("KAM icons generated (high quality).")


if __name__ == "__main__":
    main()