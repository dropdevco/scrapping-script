# Chisme — Engineering Documentation

Chisme is a bilingual (EN/ES) events product for the El Paso, TX + Ciudad Juárez, MX border
region. It scrapes events from ~30 public sources, moderates and stores them, publishes them on a
Next.js site, renders them into daily Instagram carousels with a human approval loop, and exports
them to a Google Sheet that grounds a customer-facing chatbot.

**This directory is the source of truth for how the system works.** If a doc here and an older
file disagree, this directory wins. If this directory and the code disagree, the code wins — and
the doc is a bug (see [Keeping these docs true](#keeping-these-docs-true)).

---

## Start here

| If you are… | Read, in order |
|---|---|
| **New to the team** | [Onboarding](onboarding.md) → [Architecture overview](architecture/overview.md) → [Invariants](architecture/invariants.md) → the component doc for your first task |
| **Picking up a first task** | [Known gaps](known-gaps.md) — a triaged backlog with file references |
| **Debugging production** | [Runbooks](operations/runbooks.md) |
| **Confused by a term** | [Glossary](glossary.md) |
| **Wondering "why is it like this?"** | [Decision records](architecture/adr/) |

New engineers: budget about three hours for Onboarding + Overview + Invariants. Those three are
the only documents everyone is expected to have read.

---

## Map

### Foundations

| Doc | What it answers |
|---|---|
| [onboarding.md](onboarding.md) | What do I install, what access do I need, what do I read, what do I do first? |
| [glossary.md](glossary.md) | What does *digest*, *horizon*, *slot*, *period key*, *kind*, *slide key* mean here? |
| [known-gaps.md](known-gaps.md) | What is known-broken, unfinished, or unverified — and how bad is each? |

### Architecture

| Doc | What it answers |
|---|---|
| [architecture/overview.md](architecture/overview.md) | What are the pieces, how does data flow between them, what runs where? |
| [architecture/invariants.md](architecture/invariants.md) | Which rules must never be broken, why, and what breaks if you break them? |
| [architecture/adr/](architecture/adr/) | Why was each significant decision made, and what was rejected? |

### Components

| Doc | Covers |
|---|---|
| [components/scraper-engine.md](components/scraper-engine.md) | `src/scraper/core/`, `src/scraper/sources/`, MCP server, scheduler, backfill scripts |
| [components/social-pipeline.md](components/social-pipeline.md) | `src/scraper/social/` — selection, rendering, Telegram approval, publishing, metrics |
| [components/web-app.md](components/web-app.md) | `web/` — Next.js 16 site, admin surfaces, auth, map, i18n, design system |
| [components/knowledge-base-export.md](components/knowledge-base-export.md) | `src/scraper/kb/` — the Google Sheet that grounds the GoHighLevel bot |

### Data

| Doc | Covers |
|---|---|
| [data/schema.md](data/schema.md) | Every table, column, index, constraint, RLS policy, and storage bucket |
| [data/migrations.md](data/migrations.md) | Migration inventory and the procedure for shipping a schema change |

### Operations

| Doc | Covers |
|---|---|
| [operations/configuration.md](operations/configuration.md) | Every environment variable: consumer, default, provider, and what breaks without it |
| [operations/ci-cd-and-deployment.md](operations/ci-cd-and-deployment.md) | The two GitHub Actions workflows, deployment topology, how a change reaches production |
| [operations/runbooks.md](operations/runbooks.md) | Incident playbooks: no events, Telegram silent, token dead, sheet failed, site broken |

### Engineering practice

| Doc | Covers |
|---|---|
| [engineering/conventions.md](engineering/conventions.md) | Commit style, branching, the verification contract, code and comment style |
| [engineering/testing.md](engineering/testing.md) | Test layout, commands, conventions, reusable fakes, coverage gaps |

### Third-party setup playbooks (pre-existing, still accurate)

| Doc | Covers |
|---|---|
| [meta-instagram-onboarding.md](meta-instagram-onboarding.md) | Standing up a Meta app and Instagram Business token from zero |
| [social-automation-onboarding.md](social-automation-onboarding.md) | Telegram bot, Vercel env, GitHub dispatch token — with every gotcha hit live |
| [ghl-ig-comment-bot-prompt.md](ghl-ig-comment-bot-prompt.md) | The GoHighLevel comment-to-DM automation that consumes the KB sheet |

---

## Superseded documents

These predate this directory and contain claims that are now wrong. They are kept for their
historical record, not as references.

| File | Status |
|---|---|
| `PROJECT_HANDOFF.md` | **Historical.** Its change log (through 2026-08-06) is a genuine record and worth reading for context. Its factual sections are stale — wrong migration count, wrong map library, wrong theme, wrong workflow shape. |
| `HANDOFF.md` | **Historical.** Written 2026-08-21; wrong about migration count, test scope, the map library, and whether Instagram posting is live. |
| `web/SETUP.md` | **Partially stale.** Its environment-setup steps are still useful; its "Design System", "Key Features", and "Next Steps" sections describe a version of the app that no longer exists. |
| `README.md` (repo root) | Accurate on the scraper; its Chisme section is thin. Treat this directory as the expansion. |

The authoritative record for everything after 2026-08-06 is the **git commit bodies**, which are
unusually detailed, plus this directory.

---

## Keeping these docs true

The single biggest documentation failure in this project's history was drift: a `PROJECT_HANDOFF.md`
that new readers were instructed to trust, which had stopped matching reality months earlier. The
rules below exist so that does not happen again.

**1. Every doc carries a verification stamp.** The footer of each file names the commit it was
verified against. When you touch a doc, update the stamp. A stale stamp is a warning to the reader,
which is strictly better than silent wrongness.

**2. Code comments are canonical; docs are a map.** This codebase writes unusually good comments —
they record real incidents, measurements, and rejected alternatives. Docs here quote and point at
them rather than paraphrasing. When they conflict, the comment wins and the doc gets fixed.

**3. Docs change in the same commit as the code.** If your change makes a sentence here false,
fix the sentence in that commit. This is part of the [verification contract](engineering/conventions.md#the-verification-contract).

**4. Decisions are appended, never edited.** An ADR records what was decided *at a point in time*.
When a decision is reversed, write a new ADR that supersedes the old one and mark the old one
`Superseded by ADR-NNNN`. Do not rewrite history — the rejected alternatives are the valuable part.

**5. Prefer a file reference to a copied code block.** `selection.py:127-135` stays correct across
refactors in a way that a pasted snippet does not.

**6. Refresh the knowledge graph.** After code changes, run `graphify update .` so `graphify query`
stays a reliable way to orient. It is AST-only and costs nothing.

---

*Verified against commit `9157646` (2026-09-06). Last updated 2026-09-10.*
