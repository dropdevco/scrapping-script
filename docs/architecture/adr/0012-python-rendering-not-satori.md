# ADR-0012: Slides render in Python/Pillow, not `next/og`

- **Status:** Accepted
- **Date:** 2026-08-06 (`366d857`)
- **References:** `src/scraper/social/render.py:1-9`, `pyproject.toml:28-32`

## Context

The app already had a Next.js deployment with `ImageResponse` (Satori) available — the natural place to
render images in a project that already renders HTML. Rendering the carousel there would have reused
the site's existing fonts, colour tokens and component vocabulary.

## Decision

Render slides in Python with Pillow, in `src/scraper/social/render.py`.

**The deciding constraint is one line:** Instagram's Content Publishing API **accepts JPEG only and
rejects PNG outright.** Satori / `ImageResponse` emits PNG. From `render.py:3-6`:

> "it is the only format Instagram's Content Publishing API accepts. That single constraint is why
> rendering lives here in Python rather than reusing the web app's Next.js `ImageResponse`, which emits
> PNG."

JPEG is not a style choice here.

Supporting reasons:

- The publisher already lives in Python, on the GitHub Actions runner that builds the post. Rendering
  there keeps the whole build in one process with one failure domain.
- Pillow ships manylinux wheels with no system dependencies, encodes JPEG natively, and decodes
  whatever the source CDN served — WebP, PNG with alpha, CMYK JPEG, AVIF all collapse to the same
  thing after `convert("RGB")`.
- Pillow gives pixel-level measurement (`font.getbbox`), which the layout engine needs: the whole
  "nothing cropped, nothing ellipsised" guarantee is built on measuring text and photos and *then*
  deciding the composition.

`social` is its own optional extra *"so the image job doesn't drag pandas in via `[trends]`"*.

## Alternatives rejected

| Rejected | Why |
|---|---|
| `next/og` / Satori in the web app | Emits PNG; Instagram rejects PNG. Would need a transcode step anyway, and would split the build across two runtimes and two deploys. |
| Render PNG then transcode to JPEG | Adds a step and a dependency to reach the same place Pillow reaches directly, while moving layout into a runtime the publisher cannot call. |
| Headless Chrome screenshots | A browser in the build, for a static 1080×1350 image. Far heavier, slower, and harder to measure text in. |
| An external image-generation service | A network dependency and a cost in the critical path of a daily job, for something Pillow does locally. |
| Sharing the web app's design tokens | Partially done anyway — the colour tokens are shared by value. The typography deliberately diverges (see below). |

## Consequences

**Two rendering systems, deliberately divergent typography.** The carousel's display face is Anton;
the site's is Fraunces. The colour tokens are shared by value, not by import. The lineage is recorded
in `render.py:81-87`: Cosmopolitan's masthead face (Franklin Gothic Extra Condensed) is commercially
licensed and unvendorable, Oswald is Google's rework of Alternate Gothic from the same ATF family, and
Anton is that style at a true black weight since Oswald tops out at 700.

**Fonts must be vendored as static instances.** Four TTFs in `assets/fonts/`:
`Anton-Regular.ttf`, `Archivo-Black.ttf`, `Archivo-SemiBold.ttf`, `Oswald-Bold.ttf`.
`ImageFont.truetype()` on a **variable** font silently renders the Regular instance with no error —
see [invariants §5](../invariants.md#fonts-must-be-static-instances-never-variable-fonts).

**A missing font degrades rather than fails.** `font()` logs a warning once per role and falls back to
Pillow's bitmap font, so slides still publish — off-brand. The warning exists *"so a CI run can't
quietly ship off-brand slides"*, but nothing blocks it.

**No icon font or SVG rasteriser is vendored**, so every icon-shaped mark is drawn from primitives:
the map pin, the halftone circles, the torn edges, the tape strips, and the cover's swipe triangle —
the last one drawn rather than typed because *"'→' is missing from plenty of fonts and renders as a
tofu box, which is worse than no arrow at all."*

**Encoding is pinned and asserted.** `quality=88, optimize=True, progressive=False`, with a hard
assertion on the 1080×1350 canvas at the end of every render. `progressive=False` is deliberate: some
server-side fetchers handle progressive JPEG poorly, and Meta cURLs these itself.

**`imaging.encode_jpeg` exists separately from `render._encode`** precisely because the latter asserts
the canvas size; the former keeps a human-supplied original at its own dimensions so a later re-render
can lay it out afresh.
