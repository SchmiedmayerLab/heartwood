# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Generate every Heartwood mark variant from one geometry and the shared color tokens."""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from pathlib import Path

from heartwood.cli._brand import (
    DARK,
    LIGHT,
    MARK_CORE,
    MARK_RING,
    MARK_TILE,
    NAME,
    TAGLINE,
    Palette,
)

ROOT = Path(__file__).resolve().parents[3]
BRAND = ROOT / "documentation" / "assets" / "brand"
FONT = "Inter, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif"
# REUSE-IgnoreStart
SVG_LICENSE = (
    "<!--\n"
    "This source file is part of the Heartwood open-source project\n"
    "SPDX-FileCopyrightText: 2026 Stanford University and the project authors "
    "(see CONTRIBUTORS.md)\n"
    "SPDX-License-Identifier: MIT\n"
    "-->\n"
)
# REUSE-IgnoreEnd

# Growth rings as fractions of the mark radius; the core is the heartwood.
CORE = 0.19
RINGS = (0.3, 0.44, 0.57, 0.69, 0.8, 0.9)
COMPACT_RINGS = (0.44, 0.66, 0.88)


def ring(cx: float, cy: float, radius: float, seed: int, wobble: float, points: int = 36) -> str:
    """Return a closed, slightly organic, nearly concentric ring as a cubic path."""
    coordinates = []
    for index in range(points):
        angle = 2 * math.pi * index / points
        noise = (
            math.sin(3 * angle + seed * 1.7) * 0.55
            + math.sin(5 * angle + seed * 0.9) * 0.3
            + math.sin(2 * angle + seed * 2.3) * 0.45
        )
        r = radius * (1 + wobble * noise)
        coordinates.append(
            (cx + r * math.cos(angle) + radius * 0.035, cy + r * math.sin(angle) * 0.97)
        )
    count = len(coordinates)
    parts = [f"M{coordinates[0][0]:.2f} {coordinates[0][1]:.2f}"]
    for index in range(count):
        p0 = coordinates[index - 1]
        p1 = coordinates[index]
        p2 = coordinates[(index + 1) % count]
        p3 = coordinates[(index + 2) % count]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        parts.append(f"C{c1[0]:.2f} {c1[1]:.2f} {c2[0]:.2f} {c2[1]:.2f} {p2[0]:.2f} {p2[1]:.2f}")
    return "".join(parts) + "Z"


def mark_shapes(
    cx: float,
    cy: float,
    radius: float,
    *,
    rings: tuple[float, ...],
    ring_color: str,
    core_color: str,
    stroke: float,
    fade: float,
) -> list[str]:
    """Return the core and ring elements of the mark."""
    shapes = [f'<path d="{ring(cx, cy, radius * CORE, 11, 0.03)}" fill="{core_color}"/>']
    for index, fraction in enumerate(rings):
        opacity = 1 - index * fade
        shapes.append(
            f'<path d="{ring(cx, cy, radius * fraction, 3 + index * 5, 0.012)}" fill="none" '
            f'stroke="{ring_color}" stroke-opacity="{opacity:.2f}" '
            f'stroke-width="{radius * stroke:.2f}"/>'
        )
    return shapes


def tile_gradient(identifier: str) -> str:
    """Return the diagonal gradient of the mark tile."""
    return (
        f'<linearGradient id="{identifier}" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{MARK_TILE[0]}"/>'
        f'<stop offset="1" stop-color="{MARK_TILE[1]}"/></linearGradient>'
    )


def svg(width: int, height: int, label: str, body: list[str], defs: Sequence[str] = ()) -> str:
    """Wrap elements in a licensed, labeled SVG document."""
    definitions = f"  <defs>{''.join(defs)}</defs>\n" if defs else ""
    content = "\n".join(f"  {line}" for line in body)
    return (
        f"{SVG_LICENSE}"
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="{label}">\n'
        f"  <title>{label}</title>\n{definitions}{content}\n</svg>\n"
    )


def full_mark() -> str:
    """Return the full-color mark on its tile."""
    body = ['<rect width="512" height="512" rx="112" fill="url(#tile)"/>']
    body += mark_shapes(
        256,
        256,
        200,
        rings=RINGS,
        ring_color=MARK_RING,
        core_color=MARK_CORE,
        stroke=0.052,
        fade=0.11,
    )
    return svg(512, 512, NAME, body, [tile_gradient("tile")])


def compact_mark() -> str:
    """Return the compact mark for favicons and small sizes."""
    body = ['<rect width="64" height="64" rx="14" fill="url(#tile)"/>']
    body += mark_shapes(
        32,
        32,
        26,
        rings=COMPACT_RINGS,
        ring_color=MARK_RING,
        core_color=MARK_CORE,
        stroke=0.1,
        fade=0.2,
    )
    return svg(64, 64, NAME, body, [tile_gradient("tile")])


def monochrome_mark() -> str:
    """Return the single-color mark that follows the surrounding text color."""
    body = mark_shapes(
        256,
        256,
        232,
        rings=RINGS,
        ring_color="currentColor",
        core_color="currentColor",
        stroke=0.052,
        fade=0.11,
    )
    return svg(512, 512, NAME, body)


def hero(palette: Palette, *, dark: bool) -> str:
    """Return the README hero for one appearance."""
    width, height = 1280, 360
    background = ("#151516", "#0a0a0a") if dark else ("#ffffff", "#f3f4f5")
    border = "#2c2c2f" if dark else "#e3e3e8"
    echo_opacity = 0.14 if dark else 0.12
    echoes = [
        f'<path d="{ring(250, 180, radius, 40 + index * 7, 0.018)}" fill="none" '
        f'stroke="{palette.primary}" '
        f'stroke-opacity="{max(0.02, echo_opacity - index * 0.012):.3f}" '
        f'stroke-width="1.5"/>'
        for index, radius in enumerate(range(170, 1100, 62))
    ]
    tile = [
        '<g transform="translate(130 60)">',
        '<rect width="240" height="240" rx="56" fill="url(#tile)"/>',
        *mark_shapes(
            120,
            120,
            94,
            rings=RINGS,
            ring_color=MARK_RING,
            core_color=MARK_CORE,
            stroke=0.052,
            fade=0.11,
        ),
        "</g>",
    ]
    body = [
        '<g clip-path="url(#frame)">',
        f'<rect width="{width}" height="{height}" fill="url(#background)"/>',
        *echoes,
        "</g>",
        f'<rect x="0.75" y="0.75" width="{width - 1.5}" height="{height - 1.5}" rx="23.5" '
        f'fill="none" stroke="{border}" stroke-width="1.5"/>',
        *tile,
        f'<text x="428" y="182" font-family="{FONT}" font-size="84" font-weight="700" '
        f'letter-spacing="-2" fill="{palette.foreground}">{NAME}</text>',
        f'<text x="432" y="230" font-family="{FONT}" font-size="26" '
        f'fill="{palette.muted_foreground}">{TAGLINE}</text>',
    ]
    defs = [
        tile_gradient("tile"),
        f'<linearGradient id="background" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{background[0]}"/>'
        f'<stop offset="1" stop-color="{background[1]}"/></linearGradient>',
        f'<clipPath id="frame"><rect width="{width}" height="{height}" rx="24"/></clipPath>',
    ]
    return svg(width, height, f"{NAME}: {TAGLINE}", body, defs)


def browser_mark_module() -> str:
    """Return the compact mark geometry for inline rendering in the browser interface."""
    rings = "".join(
        f'  {{\n    d: "{ring(12, 12, 10 * fraction, 3 + index * 5, 0.012)}",\n'
        f"    opacity: {1 - index * 0.2:.1f},\n  }},\n"
        for index, fraction in enumerate(COMPACT_RINGS)
    )
    # REUSE-IgnoreStart
    return (
        "/*\n"
        " * This source file is part of the Heartwood open-source project\n"
        " *\n"
        " * SPDX-FileCopyrightText: 2026 Stanford University and the project authors "
        "(see CONTRIBUTORS.md)\n"
        " *\n"
        " * SPDX-License-Identifier: MIT\n"
        " */\n\n"
        # REUSE-IgnoreEnd
        "/**\n"
        " * Generated by packages/cli/scripts/generate_brand_assets.py.\n"
        " * Run `uv run python packages/cli/scripts/generate_brand_assets.py`"
        " after changing the mark.\n"
        " */\n\n"
        'export const HEARTWOOD_MARK_VIEW_BOX = "0 0 24 24";\n\n'
        "export const HEARTWOOD_MARK_CORE =\n"
        f'  "{ring(12, 12, 10 * CORE * 1.25, 11, 0.03)}";\n\n'
        "export const HEARTWOOD_MARK_RINGS = [\n"
        f"{rings}"
        "] as const;\n"
    )


def expected_assets() -> dict[Path, str]:
    """Return every generated asset keyed by its repository path."""
    favicon = compact_mark()
    return {
        BRAND / "heartwood-mark.svg": full_mark(),
        BRAND / "heartwood-mark-monochrome.svg": monochrome_mark(),
        BRAND / "heartwood-favicon.svg": favicon,
        BRAND / "heartwood-hero-light.svg": hero(LIGHT, dark=False),
        BRAND / "heartwood-hero-dark.svg": hero(DARK, dark=True),
        ROOT / "packages" / "webui" / "public" / "heartwood-favicon.svg": favicon,
        ROOT / "packages" / "webui" / "src" / "brandMark.generated.ts": browser_mark_module(),
    }


def main(argv: list[str] | None = None) -> int:
    """Write or check every generated asset."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Fail when committed assets are stale."
    )
    args = parser.parse_args(argv)
    stale = []
    for path, content in expected_assets().items():
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current == content:
            continue
        if args.check:
            stale.append(path.relative_to(ROOT).as_posix())
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if stale:
        print("Stale brand assets; regenerate them:", *stale, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
