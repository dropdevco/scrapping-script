# Onboarding

Your first week on Chisme. The goal of day one is not to understand the system — it is to have it
running locally and to have rendered something with your own eyes.

---

## What Chisme actually is

One sentence: **Chisme finds every public event happening in El Paso and Ciudad Juárez, and puts
them in front of people on a website, on Instagram, and through a chatbot.**

Four surfaces, one database:

| Surface | What it is | Who sees it |
|---|---|---|
| The scraper | A Python pipeline that pulls events from ~30 public sources into Supabase, daily | Nobody — it is infrastructure |
| The website | `www.epchisme.com`, a bilingual Next.js app: event list, map, detail pages, submission form | The public |
| The Instagram carousel | A rendered image post built from that day's events, approved over Telegram, published to `@elpasochisme` | The public |
| The knowledge-base sheet | A Google Sheet regenerated after every scrape, which grounds a GoHighLevel chatbot | Customers, indirectly |

The **database is the integration point**. Nothing calls anything else directly. The scraper writes
events; the website, the carousel and the sheet all read them. That is why the [data
model](data/schema.md) and the [invariants](architecture/invariants.md) matter more here than in a
typical app — a bug in the shared data reaches all three consumer surfaces at once.

---

## Day 1

### 1. Access checklist

Ask your lead for each of these. Some take a day to arrive, so request them all on day one.

| System | What you need | Needed for |
|---|---|---|
| GitHub | Write access to `dropdevco/scrapping-script` | Everything |
| Supabase | Member on the project; the URL + `service_role` key + `anon` key | Any local run that touches data |
| Vercel | Member on the `scrapping-script` project | Deploys, env vars, build logs |
| Google Cloud | Access to the OAuth client and the Maps API key | Local sign-in and the map |
| Telegram | Added to the notification chat, and your chat id in `TELEGRAM_CHAT_ID` | Seeing and approving carousels |
| Meta / Instagram | Only if you touch publishing — see [meta-instagram-onboarding.md](meta-instagram-onboarding.md) | Publishing |
| `ADMIN_EMAILS` | Your Google account email added to the Vercel env var | The `/admin` moderation queue |

You can do a great deal with only GitHub + Supabase. Do not block on the rest.

### 2. Local setup — Python (the scraper)

Requires Python 3.11+ (CI runs 3.12).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[trends,dev,social,kb]"
cp .env.example .env
```

Then fill in `SUPABASE_URL` and `SUPABASE_KEY` in `.env`. Everything else is optional by design —
a source with no key disables itself and the run continues without it
(`.env.example:2-3`). See [configuration](operations/configuration.md) for the full inventory.

Verify:

```bash
pytest
```

239 tests, about a second. If ~9 files fail on import, you skipped the `social` extra (Pillow).

```bash
python -c "from scraper.sources.registry import status; [print(s) for s in status()]"
```

That prints every source and whether it is currently active. With only Supabase configured you
should still see the keyless sources (`events_web`, `events_directories`, `trends_hackernews`,
`web_ddg`, `local_news_feeds`) as `active: True`.

### 3. Local setup — Node (the website)

Requires Node 20.9+.

```bash
cd web
npm install
cp .env.example .env.local
```

Fill in `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`, and
`NEXT_PUBLIC_GOOGLE_MAPS_API_KEY`. Then:

```bash
npm run dev
```

Note `.claude/launch.json` sets `"autoPort": true`, so the dev server may not land on 3000. Google
OAuth's dev callback is registered against `http://localhost:3000/auth/callback` specifically — if
sign-in fails locally, free port 3000 and set `autoPort` to `false`.

### 4. Render a carousel — the fastest way to see the whole system at once

```bash
python -m scraper.social build --dry-run --out ./_preview
```

This reads today's approved events from Supabase, picks the ones worth posting, renders JPEG
slides, writes them plus `caption.txt` to `./_preview`, and touches nothing else — no upload, no
database write, no Telegram message. Open the images.

This one command exercises selection, rendering, the photo quality gate and captioning. It is the
single most useful thing to run when you change anything in `src/scraper/social/`.

If it prints something like *"9 approved El Paso event(s) today / 1 event(s) have a usable photo /
skipping: only 1 usable slide(s), minimum is 4"* — that is the system working correctly. See
[runbooks](operations/runbooks.md#the-daily-scrape-produced-nothing-or-the-carousel-skipped).

---

## Days 2–3: the reading path

**Everyone reads these three, in this order:**

1. [Architecture overview](architecture/overview.md) — the pieces and how data moves between them.
2. [Invariants](architecture/invariants.md) — the rules that must not break. This is the most
   important document in the repo. Most production incidents in this project's history were a
   violation of one of them.
3. [Glossary](glossary.md) — skim it; come back when a word stops making sense.

**Then, by where you are heading:**

| Your first area | Read |
|---|---|
| Scraping, sources, data quality | [scraper-engine.md](components/scraper-engine.md), then [ADR-0002](architecture/adr/0002-local-timestamp-invariant.md) and [ADR-0005](architecture/adr/0005-region-restriction-everywhere.md) |
| Instagram carousels | [social-pipeline.md](components/social-pipeline.md), then [ADR-0003](architecture/adr/0003-human-in-the-loop-then-opt-out.md) and [ADR-0004](architecture/adr/0004-crash-safety-over-retry.md) |
| The website | [web-app.md](components/web-app.md), then `web/AGENTS.md` — Next.js 16 differs from what you probably know |
| Database or schema work | [schema.md](data/schema.md) and [migrations.md](data/migrations.md) |
| Keeping it running | [runbooks.md](operations/runbooks.md), [ci-cd-and-deployment.md](operations/ci-cd-and-deployment.md), [configuration.md](operations/configuration.md) |

### Two habits to pick up immediately

**Read the code comments.** This codebase's comments are not "what" comments — they record real
incidents, with measurements and dates, and they say what was tried and rejected. When a doc here
points at `render.py:420-429`, go read it. The comment will be better than any summary.

**Use the knowledge graph before grepping.** There is a graph at `graphify-out/`:

```bash
graphify query "how does an event get from a source into the database"
graphify explain "content_hash"
graphify path "fetch_photo" "publish_carousel"
```

It returns a scoped subgraph — usually far smaller than the raw grep output — and it is the
project's standing convention. Run `graphify update .` after you change code.

---

## Week 1: your first contribution

Pick from [known-gaps.md](known-gaps.md). It is triaged, each entry names the file and line, and
the small ones are genuinely small. Good first candidates are marked there.

Before you open a PR, satisfy the [verification contract](engineering/conventions.md#the-verification-contract).
Summarised:

- **Python:** run `pytest` and `ruff check .`. If you touched a source, confirm it self-disables
  cleanly with its keys removed. If you touched selection or rendering, run the `--dry-run --out`
  preview and look at the images.
- **Web:** run `npm run lint`; run `npm run build` for anything touching routing, server
  components, Supabase queries or `next.config.ts`. Check mobile width, not just desktop.
- **Docs:** if your change made a sentence in `docs/` false, fix it in the same commit.

Write the commit message in the house style — imperative subject describing the fixed *behaviour*,
and a body structured as symptom → measurement → root cause → what you rejected → consequence.
Read `git log` for twenty minutes; the recent commits are the specification.

---

## Things that will confuse you in week one

A short list of the surprises most likely to cost you an afternoon. Each is explained in full
elsewhere; this is the index of "you are not going mad".

| Surprise | Reality |
|---|---|
| An event exists in the DB but 404s on its own page | Every web query applies a region filter, including `fetchEvent`. See [ADR-0005](architecture/adr/0005-region-restriction-everywhere.md). |
| You "fixed" a timezone in a scraper and broke things | Timezone normalisation is applied centrally in `orchestrator.run`, deliberately, so connectors cannot regress it. See [ADR-0002](architecture/adr/0002-local-timestamp-invariant.md). |
| A re-scrape does not fix a bad description or a midnight start time | The cross-run merge only fills *missing* fields; it never overwrites. That is why the `backfill_*` scripts exist. |
| `run()` returned `status: "ok"` but nothing reached the database | Storage swallows and logs its own write failures by design. Check the logs, not the return value. |
| Two switches look like the same thing | `IG_AUTOPOST` publishes at build time with no review at all. `IG_AUTO_APPROVE` keeps the review window and only removes the required tap. They are not interchangeable. |
| The tests pass in CI | They do not. **No workflow runs `pytest`, `ruff`, or the web build.** CI only runs the scrape and the Instagram pipeline. Run tests locally. |
| `PROJECT_HANDOFF.md` says something different | It is historical. See [Superseded documents](README.md#superseded-documents). |

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
