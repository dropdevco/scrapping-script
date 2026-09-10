# Engineering Conventions

How work gets done here. Much of this is inferred from a strong, consistent practice in the git history
rather than from a written rule — which is why it is being written down.

---

## The verification contract

There is **no PR-validation workflow**: nothing in CI runs tests, lint, or the web build. What
substitutes for it is an explicit verification contract, and it is unusually specific.

### Python

- Run `pytest` and `ruff check .`.
- If you touched a **source**: confirm it self-disables cleanly with its keys and optional dependencies
  removed, and check `source_status()`.
- If you touched **selection or rendering**: run `build --dry-run --out ./_preview` and **look at the
  images.** Reviewing rendered output rather than reading render code is house practice and has caught
  several real bugs.
- If tests do not exist for the area (most of `core/`), **at minimum import the affected modules**, and
  validate against production data with a `--dry-run` backfill.
- Avoid making basic live-source functionality require Supabase.

### Web

- `npm run lint` after any TS/React/CSS change.
- `npm run build` for routing, server-component, Supabase-query or `next.config.ts` changes.
- `npm run dev` when the change is visual, and **inspect both desktop and mobile widths.** Several
  shipped bugs were mobile-only.

### Data

- Migrations: additive only. See [migrations.md](../data/migrations.md).
- Any change to stored event data is followed by `python -m scraper.kb export`.

### Docs

- If your change made a sentence in `docs/` false, fix it **in the same commit**, and update that
  file's verification stamp.

### The strongest norm: verify against reality

Visible throughout the history — a credential-less `curl -sI` against a real signed URL before trusting
Supabase Storage with Meta; reviewing actual rendered JPEGs rather than reading render code; testing
the deployed site at phone width in a real browser; running the real scheduler against production
Supabase and eyeballing the `location` output; probing Meta's supported metric set live instead of
reading its docs; confirming a new source is static HTML and allowed by `robots.txt` before wiring it
in.

**Do not trust a mechanism you have not observed working.**

---

## Commit messages

Commits from 2026-08-06 onward are the house style, and they are genuinely excellent ADR source
material. Read twenty minutes of `git log` before writing your first one.

**Subject:** imperative mood, sentence case, no type prefix, no ticket id, no trailing period.
Describes the **outcome or the fixed behaviour**, not the files touched. Real examples:

- `Stop hiding a dead access token as 'fine, non-expiring'`
- `Make Publish Now actually publish immediately`
- `Stop the horizon venue cap starving the format it filters for`
- `Take Ticketmaster's largest image, not its first`

The one exception: documentation-only commits use a `docs:` prefix.

**Body:** wrapped prose, often 20–80 lines, structured as **symptom → measurement → root cause → what
was rejected and why → consequence.** Four habits worth copying:

1. **Quantified evidence, with a measurement date.** *"30 ticketed El Paso events in the 180-240 day
   band, measured 2026-09-03"*; *"343 stored events claimed to start between 1am and 5am"*;
   *"Applied: 1014 corrected, 219 already correct, 0 skipped"*; *"17 of the last 25 drafts"*.
2. **"Rejected, and why" is almost always present**, usually as an explicit clause.
3. **Named subsections** in long bodies, and bullet lists headed things like *"Rebuild details that are
   easy to get wrong"*.
4. **Empirical claims are labelled as such** — *"probed live against a real CAROUSEL_ALBUM rather than
   taken from docs"*; *"confirmed NXDOMAIN against public DNS, not a runner flake"*.

A multi-defect commit justifies its own scope: *"They are in one commit because they share files."*

**Trailer:** `Co-Authored-By: Claude <model> <noreply@anthropic.com>` where applicable.

> Commits before 2026-07-28 are terse and low-information. One of them — `5ce8306` "rebrand" — is
> actively misleading: it bundled the crawler/SEO feature set with a filter refactor, and **no brand
> change happened in it.** The real rename was `e59ef9a`.

---

## Branching

Trunk-based with short-lived feature branches, squashed or rebased into `main` — `git log --graph`
shows linear history with no merge commits.

- `main` is the default and the deploy target. `githubDispatch.ts` hardcodes `ref: "main"`.
- Naming: `feat/<kebab-case>`, `fix/<kebab-case>`.
- Whether PRs are used in practice is **unverified** — there is no PR template, no CODEOWNERS, and no
  branch protection visible from the repo. Ask your lead.

---

## Code style

### Python

| | |
|---|---|
| Version | `requires-python = ">=3.11"`; CI runs 3.12 |
| Lint | `ruff`, `line-length = 100`, `target-version = "py311"`. That is the **entire** config — default rule set |
| Tests | `pytest`, `asyncio_mode = "auto"` |
| Layout | `src/` layout; the editable install is what makes `scraper` importable from `tests/` |
| Broad excepts | Allowed where deliberate, with an explicit `# noqa: BLE001` |

Optional extras exist to keep installs lean, and the split is intentional: `trends` (pulls pandas),
`dev`, `admin` (psycopg, for DDL), `social` (Pillow — *"so the image job doesn't drag pandas in via
`[trends]`"*), `kb` (gspread — *"so the scrape job only pays for it on the runner that actually
publishes"*).

Console scripts: `scraper-mcp`, `scraper-schedule`, `scraper-kb`. Note `apply_migration` and
`scraper.social` deliberately have none.

### TypeScript / React

`strict: true`, path alias `@/*` → `./src/*`. Tailwind 4 with an `@theme` block in `globals.css` —
**there is no `tailwind.config.ts`**. ESLint flat config extending `next/core-web-vitals` and
`next/typescript`.

**Read `web/AGENTS.md` before writing Next.js code.** Next 16 differs materially from what most people
(and most training data) expect: `middleware.ts` is now `proxy.ts`, and `params`, `searchParams` and
`cookies()` are Promises.

### Text and encoding

UTF-8 for human-facing Spanish text; ASCII for purely technical comments unless the file already
requires accents. Some older comments carry mojibake from a prior encoding issue — **do not spread
it.**

---

## Comment style — the house signature

This is the most distinctive convention in the repo and the one most worth adopting.

Comments here do not explain *what* the code does. They record:

- **The incident.** *"this collided for real on a live render: 'Friday Salsa Social with 3:00 PM'."*
- **The measurement.** *"a 200-day window returned 187 events on one page, but a 240-day window
  returned 215 across two."*
- **What was tried and rejected.** *"The first version of this used a blurred, darkened copy of the
  photo — the Instagram Stories idiom — and it read as exactly what it was: a smear."*
- **Why a subtlety exists.** *"No `\b` wrappers: a leading `\b` cannot match between a space and '#',
  which silently left 'Quiz #4' unstripped."*
- **What must not be changed.** *"Do not remove that read-back."*

Reference examples: `config.py:127-139`, `render.py:420-443`, `selection.py:267-285`,
`storage.py:271-284`, `githubDispatch.ts:21-38`, and the workflow YAML comments in `ig_daily.yml`.

**Write comments like this.** A future reader — including you — will trust the code more and change it
more safely. It is also why the ADRs in this directory were reconstructible at all.

---

## Editing rules

Carried forward from the prior handoff, and still the norm:

1. Preserve user changes in the worktree; do not reset or revert unrelated files.
2. Keep edits scoped to the requested behaviour.
3. Use the existing source/model/query/i18n/component patterns before adding new abstractions.
4. Update Supabase migrations when the stored data contract changes; do not silently rely on app-only
   schema assumptions.
5. Update the docs when a change alters how the project should be understood.

---

## Working with the knowledge graph

There is a graph at `graphify-out/` (1651 nodes). Project convention:

```bash
graphify query "<question>"     # scoped subgraph — use before grepping
graphify explain "<concept>"
graphify path "<A>" "<B>"
graphify update .               # after code changes — AST-only, no cost
```

`GRAPH_REPORT.md` is for broad architecture review; `query` / `path` / `explain` are for everything
else. **Run `graphify update .` after your changes**, or the next person's orientation is wrong.

---

## Documentation maintenance

The rules live in [docs/README.md](../README.md#keeping-these-docs-true). The short version:

- Every doc carries a verification stamp naming the commit it was checked against. Update it when you
  touch the file.
- Code comments are canonical; docs are a map. When they conflict, fix the doc.
- Docs change in the same commit as the code.
- ADRs are appended, never edited. Supersede, do not rewrite.
- Prefer a `file.py:line` reference to a pasted code block.

This exists because the previous handoff document drifted for a month while instructing readers to
trust it. That is the failure mode to avoid.

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
