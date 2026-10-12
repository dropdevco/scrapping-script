# Questions and Decisions

Every original question (Q-1 to Q-16) was **answered by the product owner on 2026-10-12**. The answers
are recorded verbatim in spirit below, with what each one changes. New questions that came out of the
answers are at the bottom (Q-17 onward) and are **open**.

When a question is answered, record it here and update [SPEC.md](SPEC.md) and [ROADMAP.md](ROADMAP.md)
in the same commit.

---

## Decisions (answered 2026-10-12)

| # | Question | Answer | What it changes |
|---|---|---|---|
| Q-1 | Weighted fusion vs. "newest reading wins" | **Show both numbers.** Today's answer (CBP / freshest source) stays, and the Chisme estimate is shown beside it | [ADR-0013](../architecture/adr/0013-weighted-fusion-of-wait-signals.md) is Accepted in this form. `consensus.py` rule 2 is **kept** for the first number, not superseded |
| Go-live gate | When does the estimate appear and drive ranking? | **Hidden until it meets the Q-6 target.** Ranking keeps today's method until then | SPEC §4.6 |
| Q-2 | CBP says closed, another signal says moving | **Flag it, don't decide.** Show "CBP says closed, traffic appears to be moving" | SPEC §4.4: a closure conflict is surfaced, never resolved silently |
| Q-2b | How to rank and show when sources disagree | **Rank on the trust-weighted estimate** (once live), not the worst case | **Supersedes** the "disputed lane is ranked on its worst case" rule (`border-wait-times.md:245-249`) once the estimate is live. Recorded in ADR-0013 |
| Q-3 | Where the always-on video worker runs | **A machine we own** | Phase 6.2. The worker must report a heartbeat (ADR-0011: loud failures) |
| Q-4 | Directions | **Both: northbound and southbound** | `direction` is required on every signal. Southbound has **no CBP number**, so it is estimated from cameras, Maps and the crowd only, and graded by southbound timed crossings |
| Q-5 | Lane types | **All of them**: car, SENTRI, Ready Lane, pedestrian, commercial | Each lane is estimated separately where a signal can tell lanes apart. Maps cannot (SPEC §5.1), so it contributes only where lanes share one approach |
| Q-5b | Bridges | **All six**: Paso del Norte, BOTA, Ysleta, Stanton, Tornillo, Santa Teresa | Tornillo and Santa Teresa have no known camera, so they rely on CBP, sites, Maps and the crowd |
| Q-6 | Accuracy target | **Stricter: average error ≤ 5 min, ≥ 80 % within 10 min**, over ≥ 30 timed crossings per bridge | Gate for showing the estimate (SPEC §8) |
| Q-7 | Camera frames from YouTube | **Use the YouTube streams anyway**, accepting the risk | Product-owner decision, recorded with its risk: YouTube's ToS and API policies forbid automated capture. The likely failures are an IP block of the worker's connection and streams that disappear. The worker must detect a dead stream and alert (SPEC §5.1) |
| Q-8 | Google Maps budget | **Free tier now; document the paid option** | 5,000 free calls a month ÷ 6 segments ≈ 833 polls a month ≈ **one poll every ~52 min** [A: 6 segments]. The paid option (every 10 min ≈ $209/month) is documented in SPEC §5.1 |
| Q-8b | Maps terms on storing data | **Accept the risk, but get a legal review first** | Phase 5 waits on the review. Who does it is Q-17 |
| Q-9 | Which websites | **All of them, after a deep investigation** of who publishes border waits, **including Facebook groups** | New Phase 4.0 (investigation). bordertraffic.com still needs a licence for commercial use. Facebook groups cannot be scraped within Meta's terms, so the investigation looks for a legitimate route (Q-18) |
| Q-10 | Crowd channels | **All four**, in order: **Telegram bot → website form → Instagram DMs + comments → WhatsApp** | Phase 3 builds them in that order |
| Q-11 | Instagram inbox shared with GoHighLevel | **Investigate first** whether our webhook and GHL can both subscribe | New step 3.0b |
| Q-12 | Meta App Review / Business Verification owner | **Prepare everything now; decide the owner later** | New step 3.0a: draft the screencast script and privacy-policy text |
| Q-13 | Crowd weight | **As proposed** (~30 % of a published site, corroboration, 25 % cap), **plus position reports**: a photo or location tells us where someone is in line, and with our queue speed that gives a deterministic wait for them. A report from near the end of the line also tells us the queue length | New signal kind in SPEC §5.4. **[A, unverified]** Instagram and most chat apps strip photo GPS, so a shared live location (Telegram, WhatsApp) is the likely reliable route (Q-19) |
| Q-14 | Privacy | **Also keep the message text**: hashed sender id, parsed values and raw text, kept **30 days** | `border_reports.raw_text`, with a 30-day prune |
| Q-15 | Surfaces | **GHL knowledge-base sheet, DM replies, website.** Instagram posts are not chosen | Phase 7.3 |
| Q-16 | Who applies migration 0019 | **Claude, via the Supabase connector**: check the live database first, then apply 0019 with an explicit go-ahead. Creating the GitHub token and storing it in Vault stays with a human | Step 0.3 |

### Notes on the original options

- Q-1's chosen option, "show both", was listed in the first draft of ADR-0013 as a rejected alternative.
  ADR-0013 was still *Proposed* at that point, so it was rewritten to record the actual decision, with
  the original proposal kept as a rejected alternative.

---

## Open questions (raised by the answers)

### Q-17. Who does the legal review of Google Maps terms?

Phase 5 waits on it (Q-8b). Is there a lawyer or advisor to send it to, or should I prepare a
one-page summary of the relevant clauses for whoever does it?

### Q-18. Facebook groups: which route?

Scraping groups breaks Meta's terms and the Groups API is effectively gone. The legitimate routes are:

- the group's admins agree to share or forward reports;
- members tag or forward reports to our Telegram bot or website;
- partnering with a group to cross-post our estimate in exchange.

Which groups matter most (for example "Reporte de Puentes")? Is there a relationship with any admin?

### Q-19. Position reports: how do people send their position?

**[A]** Photos sent through Instagram and most chat apps lose their GPS data. The options:

- shared live location (Telegram and WhatsApp support it);
- the website form asking for location permission;
- a photo that we place by landmark, which is manual and does not scale.

Which do you want? Is asking for location acceptable to the audience?

### Q-20. Southbound ground truth

With no CBP number southbound, the southbound estimate is graded only by southbound timed crossings.
Should replies invite people heading into Juárez to time their crossing too ("voy a cruzar a Juárez")?

---

*Answers recorded 2026-10-12 on branch `chisme-border`.*
