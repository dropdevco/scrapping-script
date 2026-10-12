# ADR-0013: A trust-weighted Chisme estimate is shown beside today's freshest-reading number

- **Status:** Accepted, 2026-10-12, by the product owner (answers to Q-1, Q-2, Q-2b and Q-6 in
  [QUESTIONS.md](../../border-estimator/QUESTIONS.md))
- **Date:** 2026-10-12
- **Supersedes, once the estimate is live:** the rule that a disputed lane is ranked and captioned on its
  worst case (`docs/components/border-wait-times.md:245-249`). This rule has no ADR of its own.
- **Does not supersede:** rule 2 of `src/scraper/border/consensus.py:9-11` ("minutes follow
  freshness"). It still produces the first of the two numbers.
- **References:** [SPEC.md §4](../../border-estimator/SPEC.md#4-fusion), `src/scraper/border/crossings.py:97-132`

## Context

Today every lane's shown minutes come from the freshest independent reading. That was right for two
published sources: CBP (about hourly) and pasosfronterizos (about 8 min). The fresher one is the better
description of the line, and averaging two disagreeing numbers would invent a third that nobody claimed.

The estimator adds signals of very different kinds and quality: measured queue speed (cameras, Maps),
more published sites of unknown reliability, and crowd reports that are individually unreliable.
Feeding them all into "freshest wins" breaks it. A crowd report is almost always the freshest signal,
so one DM would set the number. Freshness is also not accuracy, and `crossings.accuracy()` already
measures each source's real error.

## Decision

1. **Two numbers.** Each lane keeps today's number (CBP or the freshest source, from `consensus.py`,
   unchanged). Next to it, a **Chisme estimate** is computed. That estimate is a weighted median of
   independent signals, each weighted by trust × freshness × kind factor. Trust is earned against timed
   crossings (SPEC §4).
2. **Hidden until it is good enough.** The estimate is computed and stored on every poll, but shown
   only once it meets the accuracy target. The target is an average error of ≤ 5 min and ≥ 80 % of
   crossings within 10 min, over ≥ 30 timed crossings per bridge.
3. **Once live, ranking uses the estimate**, not the worst case of a disputed lane.
4. **Closure conflicts are flagged, never resolved.** If CBP says closed and a camera or Maps signal
   says traffic is moving, the lane shows both facts. No signal silently overrides the other.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Replace the shown number with the fused estimate** (the original proposal in this ADR) | Product owner preferred to keep the official CBP-derived number visible, so readers can compare and the estimate earns trust in public |
| **Keep "freshest wins" as the only number** | One crowd DM would set the number. Accuracy evidence goes unused |
| **Weighted mean** instead of median | One broken scraper reading 58 against three signals at about 20 drags the answer to a value nobody observed |
| **Hand-ranked trust only** | Unfalsifiable. The repo already collects the ground truth needed to measure trust; hand weights remain only as the prior |
| **A learned model (regression or ML)** | Not explainable to a follower. It needs far more labelled crossings than exist |
| **Show and rank on the estimate from day one** | Rejected for go-live: an untested estimate would decide recommendations before it is shown to be accurate |
| **Keep worst-case ranking forever** | Product owner chose trust-weighted ranking. Worst case remains the behaviour until the estimate is live |

## Consequences

- Trust is meaningless until timed crossings flow, so the crowd channels (Phase 3) gate the whole
  estimator.
- Captions and replies carry two numbers once live, and must say which is which.
- Southbound lanes have no CBP number (Q-4). For them the Chisme estimate is the only number once live.
- More state: `border_signals`, `border_estimates` and `border_source_trust`.
- Weight changes must be justified by the replay harness, not by inspection.

---

*Accepted 2026-10-12 on branch `chisme-border`.*
