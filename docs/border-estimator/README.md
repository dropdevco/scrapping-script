# Border Wait-Time Estimator

A plan to turn the border feed from "which source is freshest" into **one weighted estimate** of the
wait, built from four kinds of signal:

| Kind | What it is | Status today |
|---|---|---|
| **Measured** | Queue length ÷ queue speed, computed deterministically from bridge cameras and Google Maps traffic | Nothing exists |
| **Published** | Other websites' wait numbers (CBP, pasosfronterizos, …), each ranked by how trustworthy it has proven | 3 sources exist; no trust ranking |
| **Crowd** | Wait times people tell us in DMs and comments, weighted lightly | No inbound channel exists |
| **Ground truth** | Timed crossings ("voy a cruzar" → "ya crucé") used to *grade* every other signal | Code exists (`crossings.py`), no live channel feeds it |

| Doc | Read it for |
|---|---|
| [QUESTIONS.md](QUESTIONS.md) | **Start here.** Decisions only the product owner can make. Several phases are blocked on them |
| [SPEC.md](SPEC.md) | The contract: data shapes, the fusion maths, each signal's rules, storage, success metrics |
| [ROADMAP.md](ROADMAP.md) | Phases and the step-by-step build order, from today's code to the final product |
| [ADR-0013](../architecture/adr/0013-weighted-fusion-of-wait-signals.md) | **Proposed**: replace "newest reading wins" with weighted fusion |

**Conventions in these docs.** Every claim is tagged:

- **[V]** verified, with how and when;
- **[A]** an assumption, not yet confirmed;
- **[Q-n]** blocked on question *n* in [QUESTIONS.md](QUESTIONS.md).

Nothing tagged [A] should be built on until it is promoted to [V] or answered.

---

*Written 2026-10-12 against commit `f5aa3b8` on branch `chisme-border`. Status: proposal, not yet approved.*
