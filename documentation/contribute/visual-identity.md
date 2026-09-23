<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Visual Identity

Heartwood's mark is a cross-section of a tree trunk: growth rings around a solid core.
Heartwood is the dense center of a trunk that gives the tree its strength, and the mark stands for a stable, verifiable core that every interface shares.

![Heartwood mark](../assets/brand/heartwood-mark.svg){ width="128" }
![Heartwood compact mark](../assets/brand/heartwood-favicon.svg){ width="48" }

One generator produces every variant from the same geometry and color tokens, so the documentation, browser interface, terminal, and repository show the same identity.

## Variants

| Variant | Source | Use |
|---|---|---|
| Full-color mark | `documentation/assets/brand/heartwood-mark.svg` | Documentation logo, repository assets, and sizes of 64 pixels and larger |
| Compact mark | `documentation/assets/brand/heartwood-favicon.svg` | Favicons and sizes below 64 pixels, with fewer, heavier rings |
| Monochrome mark | `documentation/assets/brand/heartwood-mark-monochrome.svg` | Single-color contexts; it takes the surrounding text color |
| README hero | `documentation/assets/brand/heartwood-hero-light.svg` and `heartwood-hero-dark.svg` | The top of the repository README in the reader's appearance |
| Browser mark | `packages/webui/src/brandMark.generated.ts` | The session rail in the browser interface, drawn inline in the current text color |
| Terminal mark | `◉ Heartwood` | Terminal startup; terminals that cannot show the glyph print `Heartwood` |

## Colors

| Token | Light | Dark | Use |
|---|---|---|---|
| Primary | `#0b694d` | `#4cc792` | Primary actions, links, and the mark's tile |
| Mark core | `#7ad8b0` | `#7ad8b0` | The heartwood at the center of the mark |
| Surface | `#f7f7f8` | `#0a0a0a` | Page and conversation background |
| Foreground | `#1d1d1f` | `#f5f5f7` | Body text |
| Muted foreground | `#66666b` | `#98989d` | Secondary text and labels |

Surfaces are neutral; green marks the primary action and the brand, not the background.
Caution states use orange, and errors use red.
Documentation figures use green for what Heartwood owns, blue for what a platform owns, and dashed outlines for planned work.
The browser interface sets these values on the Grove design system's color variables, and the documentation sets them on the Zensical palette, so neither introduces another component library.

## Use the Mark

- Keep clear space of at least one quarter of the mark's width around it.
- Use the compact mark below 64 pixels and never render the full mark below 32 pixels.
- Do not recolor, rotate, outline, or add effects to the mark, and do not redraw its rings.
- Do not combine the mark with institutional or funder marks in one lockup; place them separately.

## Accessibility

- Every text pairing in the color table meets WCAG AA contrast of at least 4.5:1 on its surface; the mark's rings and core meet the 3:1 non-text contrast against its tile.
- The mark is decorative next to the product name and hidden from assistive technology there; on its own it carries the text alternative "Heartwood".
- The documentation and browser marks are static.
- The terminal animates its progress glyphs only on an interactive terminal with color enabled, and falls back to static ASCII dots when `NO_COLOR` is set, output is redirected, or the terminal cannot show the glyphs.

## Source and Regeneration

The color tokens and terminal mark live in `packages/cli/src/heartwood/cli/_brand.py`.
The mark geometry and every generated file come from `packages/cli/scripts/generate_brand_assets.py`:

```bash
uv run python packages/cli/scripts/generate_brand_assets.py
```

Run it after changing a token or the geometry, and commit the regenerated files.
The test suite runs the generator in `--check` mode and fails when a committed asset differs from its source.

The mark and its generated variants are original project work, distributed under the project's MIT license like the rest of the repository.
