# Carousel fonts

`scraper.social.render` loads these four files. If they're absent it falls back to
Pillow's built-in bitmap font and logs a warning — rendering still works, but the
slides will be off-brand, so vendor them before the first real post.

| File | Family / instance |
|---|---|
| `Fraunces_144pt-BlackItalic.ttf` | Fraunces 144pt, Black Italic — headlines |
| `Archivo-Regular.ttf` | Archivo Regular — body |
| `Archivo-SemiBold.ttf` | Archivo SemiBold — venue line |
| `Oswald-Bold.ttf` | Oswald Bold — condensed uppercase datelines |

These mirror the web app's type system (`web/src/app/globals.css`), so slides and
site look like the same brand.

## Download **static** instances, not the variable fonts

This is the one thing that's easy to get wrong. Google Fonts serves these three
families as **variable** fonts by default. `PIL.ImageFont.truetype()` does not
apply named instances — it renders the font's *default* master. Hand it
`Fraunces[SOFT,WONK,opsz,wght].ttf` and you silently get **Regular** where you
asked for Black Italic, with no error anywhere. The slides just quietly look wrong.

Get the static files instead:

- https://fonts.google.com/specimen/Fraunces → "Get font" → the zip's `static/` folder
- https://fonts.google.com/specimen/Archivo → same
- https://fonts.google.com/specimen/Oswald → same

Copy the four files above into this directory (they're gitignored by default —
commit them if you want reproducible CI renders, which is recommended).

All three are licensed under the SIL Open Font License 1.1; keep `OFL.txt`
alongside them when vendoring.

## Verifying

```bash
python -m scraper.social build --dry-run --out ./_preview
```

Check the log for `font ... missing` warnings. If there are none, the real fonts
loaded.
