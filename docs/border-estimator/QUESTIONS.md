# Open Questions

Decisions the plan cannot make for you. Each says **why it matters** and **what it blocks**. Where there
is a recommendation, it is labelled as one; it is not a decision until you answer.

When a question is answered, record the answer inline (`**Answer (date):** …`) and update
[SPEC.md](SPEC.md) / [ROADMAP.md](ROADMAP.md) in the same commit.

---

## A. Decisions that contradict what already exists

### Q-1. Weighted fusion vs. "newest reading wins"

`src/scraper/border/consensus.py:9-11` deliberately picks the **freshest** independent reading and
never averages. Your plan weights all signals together. Both cannot be true.

- Option 1: **Supersede the rule.** Fusion becomes the shown number. See
  [ADR-0013](../architecture/adr/0013-weighted-fusion-of-wait-signals.md) (Proposed).
- Option 2: **Keep the rule, add fusion as a second number** ("CBP says X, our estimate Y").
- Option 3: Run fusion in **shadow mode** first, logged next to the current answer, and switch only once
  it beats the freshest rule against real crossings. *(Recommended: this is the plan's default; see
  Phase 2.)*

**Blocks:** Phase 2 onwards.

### Q-2. Keep the other two consensus rules?

- *"Closures follow CBP"* (`consensus.py:5-7`). Should a camera showing moving cars ever override CBP's
  "Lanes Closed"? (Live CBP at 3 pm MDT on 2026-10-12 showed Ysleta and Stanton "Lanes Closed" **[V]**.)
- *"A disputed lane is ranked and shown on its worst case"* (`border-wait-times.md:245-249`). Keep that
  pessimism, or show the fused number?

**Blocks:** the fusion spec, SPEC §4.4.

### Q-3. "Never build on GitHub Actions only"?

Every job in this repo runs on GitHub Actions (plus pg_cron as its clock). **Video analysis needs an
always-on process**, which Actions cannot host. Where should it run (a small VM, Fly.io/Render, a box
you own), and with what monthly budget?

**Blocks:** Phase 6 (cameras).

---

## B. Scope

### Q-4. Which directions?

CBP publishes **northbound only** (`border-wait-times.md:693-694`). The FPFCH cameras show both
directions **[V]**. Is southbound (into Juárez) in scope? It would be the only southbound number in the
region, but it has no published source to grade against and no CBP structure to hang it on.

### Q-5. Which bridges and lanes?

There are six ports today: PDN, BOTA, Ysleta, Stanton, Tornillo and Santa Teresa. Lane types are car,
SENTRI, Ready Lane, pedestrian and commercial.

- Must the estimate be **per lane type**? Google Maps cannot separate SENTRI from general lanes
  **[A, from research]**, and a camera may or may not.
- Is commercial (truck) traffic in scope?
- Santa Teresa and Tornillo have **no known camera** **[V, filadepuentes.com 2026-10-12]**. Is CBP plus
  crowd enough for them?

### Q-6. What is "good enough"?

Pick a target so we know when to stop tuning and when fusion may go live. Suggested starting point,
**not a decision**: fused estimate mean absolute error **≤ 10 min**, **≥ 70 %** of crossings within
10 min, and beating the current freshest-wins answer on both, measured over **≥ 30** timed crossings
per bridge.

---

## C. Signal sources

### Q-7. Cameras: YouTube terms forbid frame capture

**[V]** The YouTube ToS forbids access "by any automated means… scrapers" without written permission.
The API Developer Policies III.E.1, III.I.8 and III.I.14 forbid downloading, isolating video and
non-API access. Grabbing frames from the FPFCH YouTube stream breaks both.

- Option A: **Ask FPFCH** (Fideicomiso de Puentes Fronterizos de Chihuahua, which runs the cameras,
  `puentesfronterizos.gob.mx/camaras-en-vivo.php`) and/or **City of El Paso International Bridges** for
  written permission or a direct RTSP/HLS feed. *(Recommended.)*
- Option B: Proceed against the ToS and accept the risk of blocking.
- Option C: Drop cameras and rely on Maps for queue length.

Who would make that contact? **Blocks:** Phase 6.

### Q-8. Google Maps: budget and terms

- **Budget.** Traffic-aware Routes bills at Pro: **$10 / 1,000 after 5,000 free per month [V, Google
  pricing page 2026-10-08]**. If we poll *every 10 min, 6 approach segments*, that is ≈ 25,920
  calls/month ≈ **$209/month**. The segment count is **[A]**; the real number depends on the segments
  chosen. Is that budget approved?
- **Terms.** Maps terms restrict caching beyond 30 days and **"creating other datasets based on Google
  Maps Content"** **[V via third-party copies; the official page did not load]**. Calibrating our model on
  stored Maps durations may breach this. Who signs off on this legally?
- Which API key? The web app's key is `NEXT_PUBLIC_` (browser-exposed). The estimator needs a
  **separate, server-only** key. Can you create one?

**Blocks:** Phase 5.

### Q-9. Which websites, and may we scrape them?

The candidates found (2026-10-12) are below; see the research table in [SPEC.md §5.2](SPEC.md#52-published-signals).

- **puentesjuarez.com**: CBP plus FPFCH; has no terms.
- **cruceya.com**: CBP plus its own estimates.
- **filadepuentes.com**: estimates plus crowd reports; its terms were **not read**.
- **alpuente.mx**: photo-backed crowd reports.
- **bordertraffic.com**: **personal use only; commercial use needs a licence [V]**.
- **bordertime.us**.

Which are in scope? Is a site with no terms "allowed"? Will you license bordertraffic?

**Blocks:** Phase 3.

### Q-10. Crowd reports: which channels?

"From anywhere" needs a concrete list. Each channel is a separate build:

| Channel | Readable today? | What it takes |
|---|---|---|
| Instagram DMs | **No [V]** | `instagram_business_manage_messages`, a webhook, an app set to Live, Advanced Access / App Review, Business Verification **[V Meta docs]** |
| Instagram comments | **No [V]** | `instagram_business_manage_comments`, same review path |
| Telegram public bot | Admin-only today **[V]** | A *second* bot (keep the admin bot locked down) |
| Website form | No | A form on epchisme.com |
| GoHighLevel | Inbound stays inside GHL **[V]** | GHL API pull or GHL webhook to us |
| WhatsApp / Facebook groups | No | WhatsApp Business API; FB group scraping is effectively impossible **[A]** |

Which ones, in which order?

### Q-11. GoHighLevel already listens to the Instagram inbox

`docs/ghl-ig-comment-bot-prompt.md` runs a comment-to-DM automation on the same account. Can our
webhook coexist with GHL's integration on one Instagram account? **Unknown.** Should border reports
flow *through* GHL to us instead?

### Q-12. Who owns the Meta app and its review?

Adding messaging and comment scopes means App Review and Business Verification. Who does the screencast
and privacy-policy work, and under which business entity?

### Q-13. How much less seriously should crowd reports count?

The spec proposes caps: one report never moves the estimate alone, crowd weight is capped at a fraction
of the total, and there is a per-person rate limit. What cap do you want? See SPEC §4.3. Also, should a
report with a photo (as alpuente.mx requires) count more?

---

## D. Data, privacy and operations

### Q-14. Storing message text and sender ids

Crowd reports mean storing Instagram/Telegram user ids and free text. `border_crossings.reporter` is
already an **opaque hash** (`0015`). Should we keep only hashes plus the parsed number (recommended),
and for how long?

### Q-15. Where does the estimate appear?

The candidates are the GHL knowledge-base sheet tab (`BORDER_KB_TAB`, **deliberately unset today**
per the handoff branch), the website (no border UI exists **[V]**), Instagram posts, and DM replies. In
what order?

### Q-16. Ops tasks only someone with console access can do

These are prerequisites, not design questions. Who will do them, and when?

1. Apply migration `0019` and store `github_dispatch_token` in Vault. Per
   `origin/docs/handoff-2026-10-11`, this was **not done**, and polling runs about 6×/day, not
   144×/day.
2. Get a Maps server key (Q-8) and Meta scopes (Q-12).

---

*Written 2026-10-12 against `f5aa3b8`.*
