# Carousel fonts

`scraper.social.render` loads these four files. If they're absent it falls back to
Pillow's built-in bitmap font and logs a warning — rendering still works, but the
slides will be off-brand, so vendor them before the first real post.

| File | Family / instance | Used for |
|---|---|---|
| `Anton-Regular.ttf` | Anton | headlines |
| `Archivo-Black.ttf` | Archivo, wght=900 | venue names |
| `Archivo-SemiBold.ttf` | Archivo SemiBold | addresses, chips |
| `Oswald-Bold.ttf` | Oswald Bold | condensed uppercase datelines |

Headlines are a deliberate Cosmopolitan homage: Cosmo's own masthead face is
Franklin Gothic Extra Condensed (Morris Fuller Benton, ATF, 1902) — a
commercial font we can't legally vendor. Oswald is Google's own reworking of
Alternate Gothic, Franklin Gothic's direct sibling from the same ATF family,
so it already had the right lineage; Anton is that same grotesque-condensed
gothic style at a true black weight, heavier than Oswald's max (700/Bold).

This diverges from the web app's own type system (`web/src/app/globals.css`,
which still uses Fraunces for headlines) — the carousel and the site are
allowed to look different now; the carousel's brief is "gossip-tabloid
newspaper clipping," not "match the site 1:1." Colors (paper/ink/cosmo
pink/pop yellow) still match across both.

## Download **static** instances, not the variable fonts

This is the one thing that's easy to get wrong. Google Fonts serves most of
these families as **variable** fonts by default. `PIL.ImageFont.truetype()`
does not apply named instances — it renders the font's *default* master. Hand
it a `[wght].ttf` and you silently get **Regular** where you asked for Bold,
with no error anywhere. The slides just quietly look wrong.

Get the static files instead:

- https://fonts.google.com/specimen/Anton → "Get font" → the zip's `static/` folder (Anton only ships one weight, so there's no variable-font trap here — it's already static)
- https://fonts.google.com/specimen/Archivo → same
- https://fonts.google.com/specimen/Oswald → same

`Anton-Regular.ttf`, `Archivo-SemiBold.ttf` and `Oswald-Bold.ttf` came from
those static folders directly. `Archivo-Black.ttf` did not — Archivo's
static folder tops out at Bold (700), it has no 900 weight — so it was
pinned from the variable font with `fontTools.varLib.instancer`:

```bash
pip install fonttools
python -c "
from fontTools import varLib
from fontTools.varLib.instancer import instantiateVariableFont
f = varLib.load_variable_font =  __import__('fontTools.ttLib', fromlist=['TTFont']).TTFont('Archivo[wdth,wght].ttf')
instantiateVariableFont(f, {'wght': 900, 'wdth': 100}, inplace=True)
f.save('Archivo-Black.ttf')
"
```

(The instancer copies the source's name-table metadata verbatim, so tools that
read the font's internal name may still report something like "Archivo
SemiBold" — that's stale metadata, not the actual weight. It doesn't affect
Pillow, which renders from the file path and ignores the name table.)

Copy the four files above into this directory (they're gitignored by default —
commit them if you want reproducible CI renders, which is recommended).

All are licensed under the SIL Open Font License 1.1; keep each `OFL-*.txt`
alongside them when vendoring.

## Verifying

```bash
python -m scraper.social build --dry-run --out ./_preview
```

Check the log for `font ... missing` warnings. If there are none, the real fonts
loaded.
