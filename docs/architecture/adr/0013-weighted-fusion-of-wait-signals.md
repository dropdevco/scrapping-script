# ADR-0013: Border wait times are a weighted fusion of signals, not the freshest reading

- **Status:** Proposed. Awaiting the product owner's answer to [Q-1 / Q-2](../../border-estimator/QUESTIONS.md#q-1-weighted-fusion-vs-newest-reading-wins)
- **Date:** 2026-10-12
- **Would supersede:** rule 2 ("minutes follow freshness") in `src/scraper/border/consensus.py:9-11`, which is
  recorded in `docs/components/border-wait-times.md:233-239` and has no ADR of its own
- **References:** [docs/border-estimator/SPEC.md §4](../../border-estimator/SPEC.md#4-fusion), `src/scraper/border/crossings.py:97-132`

## Context

Today every lane's shown minutes come from **the freshest independent reading**. That was right for two
published sources: CBP (about hourly) and pasosfronterizos (about 8 min). The fresher one is the better
description of the line, and averaging two disagreeing numbers would invent a third that nobody claimed.

The estimator adds signals of very different kinds and quality: measured queue speed (cameras, Maps),
more published sites of unknown reliability, and crowd reports that are individually unreliable.
"Freshest wins" breaks down under that mix:

- **A crowd report is almost always the freshest signal.** Under the current rule, one follower's DM
  would override CBP and every other source. The brief says crowd input should count *less*.
- **Freshness is not accuracy.** `crossings.accuracy()` already measures each source's real error.
  The current rule ignores that.

## Decision (proposed)

The shown minutes become a **weighted median** of independent signals. Each weight is
**trust × freshness × kind factor**, and trust is **earned against timed crossings** with a prior that
fades as evidence accumulates. Crowd input is additionally capped and needs corroboration. The full
maths is in SPEC §4.

Kept unchanged:

- closures follow CBP;
- mirrors never vote;
- disputed lanes are ranked on their worst case;
- the staleness and trend adjustments in `scoring.py`.

The fusion ships in **shadow mode**. It is computed and stored next to the current answer, and switched
on only when it beats the current answer against real crossings.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Keep "freshest wins"** | One crowd DM would set the number. Accuracy evidence goes unused. |
| **Weighted mean** | One broken scraper reading 58 against three signals at about 20 drags the answer to an in-between value nobody observed. The median resists that. |
| **Hand-ranked trust only** (a fixed weight per site) | Unfalsifiable. The repo already collects the ground truth needed to measure trust; hand weights remain only as the prior. |
| **A learned model (regression or ML)** | Not explainable to a follower or a reviewer. It needs far more labelled crossings than exist. It also breaks the "every adjustment travels with the answer" rule (`scoring.py:3-5`). |
| **Show both numbers** (CBP plus our estimate) | Viable, and listed as Q-1 option 2. It defers the decision rather than making it, and two numbers confuse a reply meant to "lead with the answer" (`text.py`). |

## Consequences

- Trust is meaningless until timed crossings flow. That makes a public crowd channel a prerequisite for
  the whole estimator, not an add-on.
- The answer for a lane can come from a blend, so captions must name what it was built from rather than
  "pasosfronterizos, hace 8 min".
- More state: `border_signals`, `border_estimates` and `border_source_trust`.
- Weight changes must be justified by the replay harness, not by inspection.

---

*Proposed 2026-10-12 on branch `chisme-border`.*
