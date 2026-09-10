# ADR-0008: Five post formats share one parameterised renderer

- **Status:** Accepted
- **Date:** 2026-09-03 (`ca30cac`), `horizon` added in the same commit
- **References:** `src/scraper/social/render.py:722-731`, `:1110-1123`, `src/scraper/social/selection.py:88-142`

## Context

The daily digest answers *"what's on tonight"*. It cannot answer *"what should I get tickets for"* —
a Tuesday open mic and a stadium show six months out are not the same kind of post and should not look
alike. Four formats were wanted: daily, weekend, monthly, and a six-months-out "save the date".

The obvious implementation is four render functions. The codebase already had one, heavily tuned
against real rendered output, with a test suite pinning a dozen layout guarantees.

## Decision

**One renderer, parameterised by `kind`.** Three mechanisms, all parameterisation rather than
duplication:

1. **Cover** — one `render_cover()` reading a spec table. `_COVER_SPECS` holds
   `(kicker_top, kicker_bottom, halo_accent)` per kind (`render.py:725-731`). Stated reason:
   *"One layout parameterised rather than four near-duplicate functions: … the thing that actually
   distinguishes 'tonight' from 'book this now' is the words and the colour, not the grid."*
2. **Event slides** — the same three builder functions, **reordered** per kind. `_BUILDERS_BY_KIND`
   permutes the tuple so *"a weekend post does not open with the same composition as that morning's
   daily one. Reordering rather than inventing new layouts keeps every format inside the same visual
   system — four unrelated designs would read as four accounts"* (`render.py:1112-1115`).
3. **Caption and selection** — `_OPENERS_BY_KIND`, `_SUBHEADS`, `_KIND_HASHTAGS` in `caption.py`; and
   `ScoreProfile` replacing hardcoded weights in `score_event`.

Window selection is likewise uniform: `weekend_bounds`, `month_bounds` and `horizon_bounds` all return
the same `(start, end)` shape as `day_bounds`, *"so the builder looks one up by kind and stays ignorant
of which format it renders"*.

### The backwards-compatibility discipline

Every per-kind table keeps `digest` **byte-identical** to the pre-kinds behaviour. Three examples:

- `"digest": _SLIDE_BUILDERS` — *"Identical tuple AND order -> byte-identical output to before this
  existed."*
- `PROFILES["digest"] = ScoreProfile()` — *"Byte-for-byte today's behaviour"*, which is why every
  pre-existing selection test passes untouched.
- `kind` defaults to `"digest"` on `render_event_slide` *"so every existing call site — and the whole
  existing test suite — keeps producing byte-identical slides."*

Three tests pin exactly this: `test_digest_cover_is_unchanged_by_the_kind_parameter`,
`test_digest_slides_are_unchanged_by_the_kind_parameter`,
`test_digest_caption_is_unchanged_by_the_kind_parameter`.

`_period_label` returns `None` for the daily post on purpose — *"passing a label there would change
output that is currently pinned by tests and by the look of the account."*

## Alternatives rejected

| Rejected | Why |
|---|---|
| Four near-duplicate renderers | Four unrelated designs *"would read as four accounts"*, and every layout fix would need applying four times. |
| Four genuinely new layouts per format | Same problem, plus it discards a composition that was tuned against real output. |
| A template/config file driving layout | Over-engineering for five formats, and it would move layout decisions out of the place the tests can reach. |
| Letting the digest's output shift "a little" | The account's visual identity and a dozen pinned tests both depend on it. Byte-identity made the change reviewable. |

## Consequences

**Per-kind tuning lives in data, not code paths.** The full `ScoreProfile` matrix:

| Field | digest / breaking | weekend | monthly | horizon |
|---|---|---|---|---|
| `ticket_bonus` | 1.5 | 1.5 | 3.0 | 4.0 |
| `evening_bonus` | 1.0 | 1.5 | 1.0 | 1.0 |
| `recurrence_penalty` | 2.0 | 2.0 | 4.0 | 5.0 |
| `recently_posted_penalty` | 2.5 | 2.5 | 2.5 | 0.0 |
| `require_ticket_links` | False | False | False | True |
| `max_per_venue` | 2 | 1 | 1 | 2 |
| `max_per_category` | 3 | 3 | 2 | 3 |

(Horizon's loose caps are [ADR-0007](0007-horizon-venue-cap.md).)

**Recurrence suppression is scoped by kind.** *"A monthly roundup is SUPPOSED to repeat what the daily
posts covered; that is what makes it a roundup, and suppressing across kinds would empty it."* Per-kind
lookback windows: digest and breaking 14 days, weekend 35, monthly 120, horizon 200. This also fixed a
latent bug where a `breaking` post's keys would have suppressed the daily digest.

**It fixed a silent latent bug.** `_variant_for` hardcoded `% 3` while `_accent_for` correctly used
`len(_ACCENTS)`, *"which made adding a fourth layout a silent no-op: the new builder would sit in the
tuple and never be reached, with nothing failing to say so."* Now pinned by
`test_variant_cycles_over_the_actual_builder_count`.

**A new `period_key` column.** Non-daily posts need a bucket for their live-uniqueness index, and it is
a stored column rather than a derived expression because `date_trunc(text, timestamptz)` is `STABLE`,
not `IMMUTABLE`, and cannot appear in an index predicate.

**`weekend_bounds` targets the weekend *on or after* `day`**, so a Thursday build covers the weekend
about to start.

**`breaking` is implemented but never scheduled.** It is in `BOUNDS_FOR_KIND` and therefore offered by
the CLI, but no cron fires it and it is absent from the workflow's dispatch choices. Reachable only
from a local invocation.
