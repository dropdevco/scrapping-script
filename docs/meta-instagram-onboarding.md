# Meta / Instagram API Onboarding — Full Playbook

**Purpose:** a complete, reusable, account-agnostic guide to connecting ANY Instagram
Business/Creator account to a Meta App for programmatic publishing (or reading). Written from a
real, first-hand walkthrough (2026-08-06, connecting `@elpasochisme`) rather than from Meta's docs
alone — every click path, error message, and gotcha here was actually hit, not assumed. Meta's UI
has already been observed to change mid-project (see Appendix A), so treat exact button labels as
"what it was called when this was written," not a permanent contract — the underlying flow (Portfolio
→ App → permissions → Roles → token → exchange) is the stable part.

**Read "Automation Feasibility" below FIRST if the goal is building an agent around this.** Not
every step can or should be automated — some are a deliberate Meta security boundary, not a
technical gap.

---

## Automation feasibility — read this before designing an agent

| Step | Feasibility | Why |
|---|---|---|
| Create/reuse a Business Portfolio | 🧑‍💻 Human, once per business | One-time setup, low value to automate |
| Create the Meta App | 🧑‍💻 Human, once per business | Reused for every account onboarded after |
| Configure permissions/use case | 🧑‍💻 Human, once per business | Same app config serves all accounts |
| Add an Instagram account as Evaluator/Tester | 🔒 Human, every account | Meta requires a human with App-Admin access to explicitly grant this |
| Account owner accepts the tester invite | 🔒 Human, every account, on the ACCOUNT'S OWN LOGIN | This is Meta's actual consent boundary — see note below |
| Generate the initial token | 🔒 Human, every account | Tied to the accepted invite above |
| Exchange short-lived → long-lived token | 🤖 Fully scriptable (once unblocked — see Appendix A) | Plain HTTPS GET, no browser needed |
| Refresh a long-lived token | 🤖 Fully scriptable | Plain HTTPS GET, no browser needed |
| Look up the real account ID | 🤖 Fully scriptable | Plain HTTPS GET (`/me`) |
| Publish content | 🤖 Fully scriptable | This is the actual point of doing all the above |
| Monitor token expiry | 🤖 Fully scriptable | Run in CI daily |

**The 🔒 rows cannot be safely or compliantly automated with browser/credential-entry bots — this
is a deliberate Meta security boundary, not something a cleverer script solves.** Automating a
login form or clicking through an OAuth consent screen on someone's behalf violates Meta's
Platform Terms, and Meta's own bot/anomaly detection actively looks for exactly this pattern,
risking the very accounts you're trying to onboard getting flagged or locked. Do not attempt to
script these steps with a headless browser pretending to be the account owner. **If the real goal
is onboarding many accounts at real scale (not just your own handful), the answer isn't
"automate around the consent screen" — it's Part 2B below (Tech Provider + App Review), where each
new account's owner clicks through ONE real, standard OAuth dialog themselves, once, and every
account after your own app-level setup only ever needs that one click — no manual developer
-console fiddling by you, ever again.**

What an agent genuinely CAN do: handle every 🤖 row end-to-end, and turn every 🔒 row into a
clear checklist/notification that pauses for a human, then resumes automatically once that human
confirms (e.g. "I've added the account and it accepted the invite — go"). That's a real, useful
amount of automation — just not literally zero human touches for a brand-new account.

---

## Part 0 — Decide your path before starting

Two fundamentally different setups, pick based on actual need:

- **Path A — Development Mode + Testers/Evaluators** (what this guide's Part 2 covers, and what
  was actually used for `@elpasochisme`). Fast, no App Review, no waiting on Meta. Ceiling: only
  accounts YOU manually add as a Tester in the developer console can ever authenticate — fine for
  your own handful of owned accounts, does not scale to onboarding other people's/clients' accounts
  without you personally doing the manual add-and-wait-for-accept dance for every single one.
- **Path B — Advanced Access via App Review** (Part 2B). Slower up front (Meta review + business
  verification), but after that, ANY Instagram Business account can connect via a standard OAuth
  consent screen with zero manual steps on your end — the account owner clicks "Allow" once, you're
  done. This is the actual path to "many accounts, minimal per-account human effort."

If unsure: start with Path A to prove the integration works end-to-end (cheap, fast, reversible),
then graduate to Path B once you know you need more than a few accounts.

---

## Part 1 — One-time business-level setup

Do this once. Every account onboarded later (Part 2 or 2B) reuses the same App.

### 1.1 Business Portfolio (Meta Business Suite)

`business.facebook.com` → Settings (gear, bottom left) → Accounts. If none exists, create one —
it's just a container that groups Pages, Instagram accounts, and (later) your App together.

### 1.2 Link the target Instagram account to a Facebook Page

Still in Business Suite → Settings → Accounts → **Instagram accounts** → Add. This works even
though this guide's actual publishing flow (Instagram Login, see 1.4) doesn't route through the
Page at runtime — Instagram's underlying account model still expects this link to exist for a
Professional account.

If the Instagram account isn't already Professional: Instagram app → Settings → Account type and
tools → Switch to professional account → **Business**.

### 1.3 Create the Meta App

`developers.facebook.com` → My Apps → **Create App**.

As of 2026-08, this is a "use case" picker, not the older Business/Consumer/Other choice — Meta
changed this mid-2026 (see Appendix A). Filter/scroll to find and check:

> **"Manage messages and content on Instagram"** — *"Post content, share stories, reply to
> comments, answer direct messages, and much more with the Instagram API."*

Do not check Marketing API, Pages API, Threads, WhatsApp, or anything else — none of those are
needed and some pull in extra review requirements for no benefit.

Fill in app name, contact email, and — important — the **same Business Portfolio** from 1.1. If
the app isn't attached to the right Portfolio, permission requests can silently fail to resolve
against the right accounts later.

**Skip "Become a technology provider" for Path A.** That's specifically for Path B (App Review) —
irrelevant and a waste of time if you're only adding your own accounts as Testers.

### 1.4 Configure the use case → grab the app's own credentials

Land on the app's Panel. Click **"Customize the 'Manage messages and content on Instagram' use
case."** Left sidebar has (labels may render in the account's UI language, e.g. Spanish):

- **Permissions and functions**
- **API configuration with Instagram Login** ← the one you want
- API Integration Assistant
- API configuration with Facebook Login (a DIFFERENT, older flow — not what this guide uses)

On "API configuration with Instagram Login," near the top:

- **Instagram app identifier** — a numeric ID
- **Instagram app secret password** — masked, click Show

**Save both.** These are DIFFERENT from the Facebook App ID/Secret (Settings → Basic, still worth
grabbing too — some code paths may want either depending on which flow they hit, see Appendix C).
Do not confuse the two pairs; using the wrong one produces a distinct, clear "Error validating
client secret" error (see Appendix A) rather than silently working.

### 1.5 Grant the permissions this app needs

Left sidebar → **Permissions and functions**. This list is long (30+ entries, mixing an OLDER
`instagram_*` namespace and a NEWER `instagram_business_*` namespace that mostly duplicate each
other under different names — see Appendix B for the full table). You only need:

- **`instagram_business_basic`** — read the account's own profile/media
- **`instagram_business_content_publish`** — the actual publish permission; **this is the one that
  matters most and is easy to miss**, it does not get added by the page's own "Add all required
  permissions" button (that one only adds basic + comments + messages, not publish)
- *(Optional, costs nothing to also add)* `instagram_content_publish` — the older-namespace
  equivalent, insurance in case a different code path ends up needing it

Click **+ Add** on each row individually. Leave everything else alone (ads/catalog/pages/shopping
permissions are irrelevant here and some trigger extra verification requirements if requested).

---

## Part 2 — Per-account onboarding (Path A: Development Mode + Testers)

Repeat this whole section for every additional account.

### 2.1 🔒 Add the account as an Evaluator

Left sidebar of the main app dashboard (not the use-case sub-panel) → **App roles** → **Roles**
tab → find **"Evaluators"** (next to Administrators/Developers) → **Add people**. Provide the
Instagram account's login/username. This only SENDS an invite — it does not grant access yet.

**Do not confuse this with "Administrators."** Administrators manage the app itself (who can edit
settings); Evaluators/Testers is the list that controls which Instagram ACCOUNTS the app can act
on. These are two separate lists on the same Roles page.

### 2.2 🔒 The account owner must accept the invite

On the phone/browser logged into the target Instagram account: Instagram app → Settings → **Apps
and websites** → **Tester Invites** tab (separate from the "Active" tab, which only shows apps
ALREADY fully authorized). Accept the pending invite there.

**This step is easy to silently skip and was the actual cause of a confusing failure during
onboarding** — basic read calls worked without it, but the account wasn't fully authorized until
this was done. If a later step behaves inconsistently (some calls work, others mysteriously
don't), check this tab before assuming anything else is wrong.

### 2.3 🔒 Generate the initial (short-lived) token

Back on "API configuration with Instagram Login" → section **"2. Generate access tokens"** → **Add
account** → pick the now-authorized Instagram account → **Generate token**.

This token is **short-lived (~1 hour)**. Meta does not redisplay it after this point (shown once in
a popup/modal at generation time) — copy it immediately, and immediately proceed to 2.4. Everything
from here on is scriptable, so an agent picking up at this exact point can do the rest unattended.

### 2.4 🤖 Look up the REAL account ID — do not trust the console's own table

The account-linking table on that same page (from 2.3) displays a numeric ID next to the account
username. **This may not be the ID the live API actually uses.** Verified case: the console showed
`17841442732011819`; a live `/me` call against the same token returned `28378571795112753` — a
DIFFERENT number. Always confirm with a live call, never hardcode the console's displayed value:

```bash
curl -s "https://graph.instagram.com/v21.0/me?fields=id,username&access_token=${SHORT_LIVED_TOKEN}"
# {"id":"<the real id>","username":"<handle>"}
```

Use the `id` from this response everywhere downstream (`IG_BUSINESS_ACCOUNT_ID`, publish endpoint
paths), not the console table's number.

### 2.5 🤖 Exchange for a long-lived token

```bash
curl -s "https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret=${INSTAGRAM_APP_SECRET}&access_token=${SHORT_LIVED_TOKEN}"
# {"access_token":"<60-day token>","token_type":"bearer","expires_in":5184000}
```

Uses the **Instagram app secret** from 1.4, not the Facebook App Secret. **As of this writing this
call has been unreliable for tokens generated via the console's "Generate token" button
specifically — see Appendix A before assuming your own attempt failed due to a mistake.** If it
fails with `"Session key invalid..."` even with the correct secret and a fresh token, that is a
KNOWN open issue, not necessarily something wrong with your inputs.

### 2.6 🤖 Verify with a real, harmless API call before trusting the token

Don't just trust that 2.5 "should" have worked — prove it:

```bash
# 1. Create a real container (uses a small test image you control, e.g. already in your own
#    storage with a signed URL Meta can fetch)
curl -s -X POST "https://graph.instagram.com/v21.0/${IG_USER_ID}/media" \
  --data-urlencode "image_url=${TEST_IMAGE_URL}" \
  --data-urlencode "access_token=${TOKEN}"
# {"id":"<container id>"}

# 2. Confirm it actually processed
curl -s "https://graph.instagram.com/v21.0/${CONTAINER_ID}?fields=status_code,status&access_token=${TOKEN}"
# {"status_code":"FINISHED", ...}

# Stop here unless you actually intend to publish — do NOT call media_publish on a container
# you were only using to test the pipeline. A container left unpublished simply expires
# harmlessly in 24h; publishing it creates a REAL, PUBLIC, IRREVERSIBLE post.
```

If step 1 succeeds even while the long-lived exchange (2.5) is failing, that tells you the SHORT
-LIVED token is fully functional for real work right now — useful for proving a build end-to-end
even before the long-lived puzzle is solved, but not a substitute for actually solving it before
relying on unattended, scheduled automation (a 1-hour token cannot survive a daily cron).

---

## Part 2B — Per-account onboarding (Path B: Advanced Access / App Review)

Not yet exercised end-to-end in this project — documented from Meta's published flow for when
scale beyond a handful of accounts is actually needed. High-level shape, since Meta's App Review UI
changes frequently enough that a literal click-path here would likely be stale quickly:

1. In the app dashboard, **"Become a technology provider"** (skipped for Path A, required here) —
   involves a business-verification step with Meta.
2. Request **Advanced Access** for `instagram_business_content_publish` (and any other permission
   actually used) — this is the App Review submission. Meta typically wants a screencast
   demonstrating the actual publish flow working in Development Mode first (which Part 2A already
   proves), plus a written use-case description.
3. Once approved, set up **"Business Login for Instagram"** — a real OAuth redirect flow:
   - Register a redirect URI (an HTTPS endpoint you control, e.g. a Next.js API route)
   - Direct a new account's owner to:
     `https://www.instagram.com/oauth/authorize?client_id={instagram-app-id}&redirect_uri={your-redirect-uri}&response_type=code&scope=instagram_business_basic,instagram_business_content_publish`
   - They log in and click "Allow" ONCE (this is the same fundamental human click as Path A's
     "accept the tester invite" — but self-service, no manual console work from you)
   - Instagram redirects back to your URI with a `code` param; exchange that code for a token
     server-side (a different, standard OAuth authorization-code exchange — not the same as the
     `ig_exchange_token` refresh call in Part 3)
4. From here, Parts 2.4–2.6 above apply identically regardless of which path got you the initial
   token.

**Worth trying this path's redirect-code exchange as the fix for Appendix A's open mystery** — the
leading theory there is that console-issued test tokens specifically aren't exchange-eligible,
which a real authorization-code-flow token likely would not have a problem with.

---

## Part 3 — Ongoing token maintenance (fully scriptable, run in CI)

```bash
# Refresh a long-lived token before it expires. Needs the token to be at least 24h old and
# not yet expired — does NOT need the app secret at all, simpler than the initial exchange.
curl -s "https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=${LONG_LIVED_TOKEN}"
# {"access_token":"<fresh 60-day token>","token_type":"bearer","expires_in":5184000}
```

Run this on a schedule well before the 60-day window closes (e.g. every 45 days) and persist the
new token wherever the old one was stored (a GitHub Actions secret cannot rewrite itself without a
separate PAT with `secrets:write` — factor that into whatever automates this).

Separately, monitor remaining lifetime so an expiring token is caught with weeks of notice instead
of surfacing as a mystery 400 error mid-cycle:

```bash
curl -s "https://graph.instagram.com/v21.0/debug_token?input_token=${TOKEN}&access_token=${TOKEN}"
```

---

## Appendix A — Known issues / open mysteries (2026-08-06)

**Long-lived token exchange (`ig_exchange_token`) fails for console-issued tokens.** Full
diagnostic trail, so the next attempt doesn't repeat this work:

- Symptom: `{"error":{"message":"Session key invalid. This could be because the session key has
  an incorrect format, or because the user has revoked this session",...}}`, immediately, even on
  a token used within seconds of generation (ruled out simple expiry).
- **Ruled out — wrong secret.** Deliberately using the WRONG secret produces a different, specific
  error: `"Error validating client secret."` The correct Instagram app secret does not trigger
  that error, meaning it IS being accepted before the flow fails for some other reason.
- **Ruled out — invalid/unauthorized token.** The exact same token works perfectly for real reads
  (`/me`) AND real writes (created a genuine media container, confirmed `status_code=FINISHED`,
  successfully called `media_publish`, got back a real `permalink`) directly against
  `graph.instagram.com`. The token is unambiguously valid and fully authorized for everything
  tested except this one specific exchange call.
- **Ruled out — unaccepted tester invite.** Checked directly: Instagram's own "Tester Invites" tab
  was empty (nothing pending), and the "Active" connections list showed the app already fully
  authorized with every needed permission toggle on, including "Publish content as a business."
- **Leading theory, NOT confirmed:** tokens from the developer console's "Generate token" button
  (built for interactive dashboard testing) may not be eligible for `ig_exchange_token` by design,
  and a token obtained via a genuine OAuth authorization-code redirect (Part 2B) may be required.
  **Start here, not from scratch, if picking this up again.**

**Meta's "Create App" wizard changed mid-project.** Originally documented (and initially given as
guidance in this same effort) as a Business/Consumer/Other app-type choice. By the time of actual
use, it had become a "use case" card picker (see Part 1.3). Lesson for this whole document: treat
exact labels/click-paths as best-effort/likely-to-drift, and treat the underlying CONCEPTS
(Business Portfolio → App → permissions → Roles → token → exchange) as the stable contract to hold
onto when the UI doesn't match this guide exactly.

---

## Appendix B — Permission reference

Only these two matter for basic publishing. Everything else in Meta's permissions list is either
irrelevant to this use case or a near-duplicate under the older `instagram_*` (non-"business")
naming Meta is gradually retiring in favor of `instagram_business_*`.

| Permission | Needed? | Purpose |
|---|---|---|
| `instagram_business_basic` | **Yes** | Read own profile/media |
| `instagram_business_content_publish` | **Yes — the critical one, easy to miss** | Create + publish media |
| `instagram_content_publish` | Optional | Older-namespace equivalent; cheap insurance |
| `instagram_business_manage_comments` | No | Only if replying to comments programmatically |
| `instagram_business_manage_messages` | No | Only if handling DMs programmatically |
| `instagram_business_manage_insights` | No | Only if pulling analytics |
| everything else (ads/catalog/pages/shopping/threads/whatsapp/branded-content) | No | Unrelated to this use case; requesting them can trigger extra review requirements for no benefit |

---

## Appendix C — API endpoint quick reference

All under `https://graph.instagram.com/v21.0` (confirmed empirically — Meta's own docs state "all
endpoints can be accessed via the graph.instagram.com host" for the Instagram Login flow; this is
DIFFERENT from `graph.facebook.com`, which is what the older Facebook-Login-based flow uses).

| Purpose | Call |
|---|---|
| Verify identity / get real account id | `GET /me?fields=id,username&access_token=...` |
| Exchange short-lived → long-lived | `GET /access_token?grant_type=ig_exchange_token&client_secret=...&access_token=...` |
| Refresh a long-lived token | `GET /refresh_access_token?grant_type=ig_refresh_token&access_token=...` |
| Check token expiry | `GET /debug_token?input_token=...&access_token=...` |
| Create a media container | `POST /{ig-user-id}/media` — `image_url`, `access_token`, `is_carousel_item=true` for a carousel child |
| Check container status | `GET /{container-id}?fields=status_code,status&access_token=...` |
| Create a carousel parent | `POST /{ig-user-id}/media` — `media_type=CAROUSEL`, `children=<comma-separated container ids>`, `caption`, `access_token` |
| Publish | `POST /{ig-user-id}/media_publish` — `creation_id`, `access_token` |
| Get the published permalink | `GET /{media-id}?fields=permalink,media_type&access_token=...` |

Hard limits worth knowing: JPEG images only (PNG rejected outright); carousel max 10 items; all
carousel items get cropped to the FIRST item's aspect ratio by Instagram itself; containers expire
24h if never published; 100 published posts / rolling 24h (a carousel counts as one).

---

## Appendix D — Building an actual onboarding agent on top of this guide

Given the feasibility table at the top, a realistic agent design:

1. **Setup wizard (run once):** walks a human through Part 1 interactively (open the right URLs,
   confirm each click), capturing the App ID/Secret and Instagram app identifier/secret at the end.
2. **Per-account onboarding queue:** for each new account, the agent:
   - Adds the Evaluator role via the Graph API directly if Meta exposes this as an API call for
     your app tier (worth checking — some Business Manager APIs do expose role management without
     a browser), otherwise surfaces a one-click checklist item for a human ("add @handle as
     Evaluator, then click Continue")
   - Pauses with a clear, specific prompt for the account-owner's acceptance step (2.2) — this
     cannot be bypassed, design the pause to be obvious and resumable, not a silent hang
   - Once resumed, runs 2.3 onward as a fully unattended script: token capture prompt → exchange →
     verify real ID → smoke test → store credentials
3. **Path B is the actual unlock for "many accounts, minimal ongoing human effort"** — build Part
   2B's redirect-callback endpoint once, and every account after that only ever needs the account
   owner to click "Allow" on a standard Instagram consent screen. That IS a form of automation
   (no manual developer-console work per account), it's just not zero-human-clicks, because Meta
   will never allow zero-human-clicks for granting access to someone's account — nor should it.
4. **Do not attempt to script the login/consent screens themselves with a headless browser.**
   Aside from being against Meta's Platform Terms, Meta's bot detection actively targets exactly
   this pattern on login-adjacent pages, and a flagged pattern risks the very accounts being
   onboarded, not just the automation attempt failing.
