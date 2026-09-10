# ADR-0009: Telegram edits record intent rows; a Python job rebuilds

- **Status:** Accepted
- **Date:** 2026-09-03 (`cf158c5`), metrics in the same cluster (`51e5f24`)
- **References:** `supabase/migrations/0009_ig_post_edits.sql`, `web/src/lib/ig/moderate.ts:136-178`, `src/scraper/social/__main__.py:327-576`

Three decisions that all turn on one constraint: **the Telegram webhook is a stateless serverless
handler.**

## Context

Reviewers wanted more than approve/reject from their phone: drop one event from the carousel, or
replace a bad photo. Two things those operations need that cancelling and caption-editing do not:

- **Pillow.** Dropping an event changes the slide count on the cover and shifts every subsequent
  slide's layout and accent, so the carousel must be re-rendered. The webhook runs as a Vercel
  serverless function and cannot run Pillow.
- **Durable state.** A photo swap is a two-step conversation (pick the event, then send the photo),
  and the handler has no session store.

## Decision

### 1. A tap records an intent; a Python job performs it

The webhook inserts a row into `ig_post_edits` (`op` ∈ `drop_event`, `swap_photo`; `payload` jsonb)
and fires a `workflow_dispatch`. `python -m scraper.social apply-edits` does the rebuild.

**The dispatch is only latency; the sweep is the guarantee.** `apply-edits` also runs unconditionally
at the top of every publish sweep, so a failed or missing dispatch costs minutes, not correctness
(`social/__main__.py:330-336`).

### 2. A table, not a jsonb column on `ig_posts`

> "Each webhook delivery is an independent invocation: two quick taps doing read-modify-write on one
> array is a **lost update**, and PostgREST offers no locking to prevent it."

### 3. Prompts carry their own state

Caption and photo edits use Telegram `force_reply` prompts with the ids **inside the prompt text**:

> "this route is a serverless handler with no session store, and Telegram hands back the entire
> message being replied to. So the post id travels inside the prompt text and comes home with the
> reply — no state to keep, nothing to expire, and it survives a cold start or a redeploy mid-edit."

Ids are recovered by regex. Relatedly, the event-picker's `callback_data` is `drp:<uuid>:<index>` = 44
bytes, inside Telegram's 64-byte limit.

### What does *not* need a rebuild

Caption editing is a plain `UPDATE`, because **the caption is never drawn onto a slide** — the renderer
never sees it, and `_publish_one` reads the column at publish time. Length is pre-checked against 2200
chars *"so the user gets a useful message from the bot rather than a Graph API error hours later at
publish."*

## Alternatives rejected

| Rejected | Why |
|---|---|
| Re-render in the webhook | Serverless handler; cannot run Pillow. |
| A jsonb array of pending edits on `ig_posts` | Two concurrent taps are a lost update, and PostgREST has no locking. |
| A session store (KV/Redis) for the edit conversation | A whole new dependency to hold state for ~30 seconds. Putting the id in the prompt text is free and survives cold starts. |
| Re-render only the changed slide and the tail | `_variant_for` and `_accent_for` are functions of slide **position**, and the cover prints the event **count**. Partial re-rendering produces an inconsistent carousel. |
| Hotlinking Telegram's photo URL | `getFile`'s URL embeds the bot token and expires in about an hour, while a later drop can trigger a rebuild days later. |
| Keying `photo_overrides` by slide index | A later `drop_event` renumbers slides, which would silently reattach someone's photo to a different event. Keyed by **event uuid** instead. |

## Consequences — the rebuild details that are easy to get wrong

All verified in `social/__main__.py:359-576` and pinned by `tests/social/test_apply_edits.py`:

- **Intents are applied in request order** (`pending_edits` orders by `requested_at`).
- **Everything is re-rendered**, cover included.
- **Events are re-read then restored to the post's stored order** — `events_by_ids` makes no order
  guarantee, and the order a human already reviewed is not one to reshuffle.
- **No re-ranking.** `candidates_from_rows` deliberately skips scoring: *"re-scoring here would
  silently reshuffle slides they already approved."* Score is left at 0.0.
- **A custom caption survives.** `caption_is_custom` suppresses regeneration so a hand-written caption
  is not overwritten.
- **Below the slide minimum, it refuses** — retires the edits and tells the human the post is
  unchanged, rather than shipping a thin carousel.
- **Orphaned objects are removed**, because `slide_paths` is positional and rewritten wholesale.
- **Swapped photos go through the same quality gate** — *"a forwarded, twice-compressed screenshot
  should be refused here rather than rendered into a blurry slide"* — and into our own bucket, under
  the post's prefix so retention sweeps it on the same schedule.
- **A final CAS on `status='draft'`.** Zero rows means the post went live mid-rebuild, so it abandons
  rather than overwriting `slide_paths` under a carousel Meta may already hold.
- **A pending edit blocks auto-approval and fails closed** — *"shipping a carousel that still contains
  the event someone explicitly asked to remove is worse than shipping late."*
- **A fresh notification is sent, not an edit of the old one** — a `sendMediaGroup` cannot be edited in
  place.

## Addendum: metrics, and why they do not feed back

Collected in the same cluster of work, for the reason opt-out posting created: *"a pipeline that ships
without anyone's say-so needs a signal that does not depend on someone remembering to open the app."*

**Metrics deliberately do not feed into selection scoring.** There is no per-slide attribution
available — a carousel is one media object and Meta exposes no per-child insight — and reach is
**6–10 per post**, far too little signal to tune category weights on without fitting noise. The
`category_bias` field exists in `ScoreProfile` as a declared seam and is unused, with a revisit
condition written down: **≥60 published posts with t24 metrics** (`selection.py:345-349`).

**The supported metric set was probed live against a real `CAROUSEL_ALBUM` rather than taken from
docs.** On `graph.instagram.com`, `impressions` is retired in favour of `views`, `navigation` is not
offered, while `profile_visits` and `follows` are. Because that set moves, `fetch_insights` drops
whatever metric name a 400 complains about and retries — terminating because each pass drops at most
one name and any other error raises.

**Columns *and* raw are both stored:** columns keep the queries trivial, `raw` means the next metric
rename costs a column of NULLs rather than the data.
