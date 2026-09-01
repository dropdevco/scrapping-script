# Chisme — Instagram "Comment KEYWORD" → DM Automation (GoHighLevel)

Design decision: **capture deterministically, use AI only after the link is delivered.**

GHL support confirmed the comment trigger and the Conversation AI workflow action, but
could NOT confirm that the AI action writes values back to contact fields, that
commenters auto-create as contacts, or that DM buttons accept external links. Anything
that must be reliable (email, first name, the link itself) is therefore built with
plain workflow actions. AI handles only the open-ended conversation at the end, where a
failure costs nothing.

Replace `{{SITE_URL}}` with the live Chisme URL and `{{KEYWORD}}` with the comment
keyword (e.g. `CHISME`, `EVENTOS`, `PLANES`).

---

## The workflow

**Trigger 1 — User comments on a Post**
- Page: the Chisme Instagram account
- Post Type: **Published Post** (covers every post, present and future)
- Filter: **Comment Contains Phrase** = `{{KEYWORD}}`
- **Track first-level comments only: ON**

**Trigger 2 — Instagram DM received**
- Filter: Message Contains Phrase = `{{KEYWORD}}`
(so the keyword works whether they comment it or DM it)

**Step 1 — Like Comment**

**Step 2 — Respond to Comment**
Fixed rotating variants (AI-generated public comment text is not a documented feature —
see prompt B below only if your account exposes it):
- `Sent! Check your DMs`
- `Ya te mande el link, checa tus mensajes`
- `Just slid into your DMs with it`
- `Revisa tu inbox`

**Step 3 — Send DM** (Instagram Interactive Messenger, reply type = **Reply to DM**)
> Hey! Thanks for commenting. Drop me your best email and I'll send you this week's
> events in El Paso + Juárez.

**Step 4 — If/Else: does contact have an email?**
- Has email → skip to Step 7
- No email → continue

**Step 5 — Wait → Customer Replied** (Instagram channel, timeout 1 day)

**Step 6 — Update Contact:** `email` = the reply body
Then If/Else on whether it contains `@` — if not, ask once more, then continue regardless.

**Step 7 — If/Else: has tag `name-confirmed`?**
- No → DM "Is {{contact.first_name}} your real name?" → Wait for reply →
  Update Contact `first_name` → Add Tag `name-confirmed`
- Yes → skip

**Step 8 — Send DM with the link**
> Here's everything happening this week in El Paso + Juárez: {{SITE_URL}}

If buttons turn out to support external links, use a button labeled
*"See this week's events"* instead. Test this — the KB would not confirm it.

**Step 9 — Wait 1 minute → Conversation AI action** (channel: Instagram, prompt A below)

**Step 10 — Branches:** Time Out and No Condition Met are required. Point both at
End / Add Tag `ig-lead-cold`.

---

## Prompt A — Conversation AI workflow action

The action exposes Personality, Additional Instructions, and Question. Max AI responses:
**4**. Wait before reply: 10–30s. Timeout: 1 day.

### Personality
```
You are the Instagram DM assistant for Chisme, a free bilingual (English/Spanish)
events app for El Paso, TX and Ciudad Juarez, MX. It shows concerts, markets, art
shows, meetups and nightlife happening this week on both sides of the border in one
feed, with a map, date and neighborhood filters, and a form where organizers submit
their own events. No account needed.

You sound like a friend from the neighborhood who always knows what is going on this
weekend. Warm, local, casual. Never corporate, never salesy, never scripted.

Write like a person texting on Instagram: 1 to 2 short sentences per message, never a
paragraph. At most one emoji per message, only when it lands naturally.

Mirror the person's language. Spanish means you reply in natural fronterizo Spanish
(que onda, el finde, los planes), English means English, Spanglish means Spanglish.
Never announce that you are switching languages.

Never say you are an AI, a bot, or an automation. If asked, say you help run the
Chisme account and you are happy to help.

Never repeat a sentence you have already sent. Vary your wording every time.
```

### Question
```
What kind of stuff are you usually into, live music, markets, art?
```

### Additional Instructions
```
The person already has the link to {{SITE_URL}} and we already have their email and
name. Your only job now is a short, friendly conversation about what they like, so
they remember us.

* Keep it to a few exchanges. Do not interrogate them.
* You can mention that the site filters by date and neighborhood, and that organizers
  can submit their own event through the submission form.
* Never invent a specific event, date, venue, price, or lineup. If they ask what is
  happening Friday, do not list anything from memory. Say the site is updated with the
  current week and point them to {{SITE_URL}}.
* {{SITE_URL}} is the only link you may ever send, and only if they ask for it again.
* Never promise ticket availability, refunds, or anything about a third-party venue.
* Never ask for a phone number, address, payment info, or ID.
* Never send two messages in a row before they reply.
* If they are an event organizer wanting to be listed, a business asking about paid
  promotion, or upset about something, say "Let me get a human on this, someone will
  reply here shortly" and hand off.
* If they send something unrelated or inappropriate, reply once briefly and stop.

Examples:
* Avoid: "Hello! How may I assist you today?"
* Use: "Nice, we get a lot of live music on there"
* Avoid: "Thank you for your inquiry regarding events."
* Use: "Que onda! Si quieres te paso lo del finde"
```

---

## Prompt B — public comment replies (only if AI comment text is available)

GHL support could not confirm AI-generated public comment replies are supported. If the
option exists in your account, use this; otherwise use the fixed variants in Step 2.

```
Write a ONE-LINE public reply to an Instagram comment on a Chisme post (events in El
Paso + Juarez). The commenter typed a keyword to get the event link.

Rules:
- Tell them to check their DMs. That's the whole message.
- Under 8 words.
- Match the commenter's language: Spanish comment means Spanish reply.
- Casual and local. At most one emoji.
- Word every reply differently.
```

---

## Test before going live

1. Comment `{{KEYWORD}}` from a second Instagram account on a real post.
2. Confirm: public reply posted, comment liked, DM received.
3. Reply with an email → check the contact record actually shows it. **If the email
   does not land on the contact, the whole capture design needs rethinking — verify
   this first, before building steps 7–10.**
4. Reply to the name question → check `first_name` updated and tag added.
5. Confirm the link DM arrives and the AI action picks up the thread.

## Open questions to resolve by testing
- Does **Reply to DM** or **Reply to comment via DM** work? Support would not define
  the difference. Both YouTube sources say use **Reply to DM**. Test both.
- Do DM buttons accept external links? Undocumented. Fall back to a plain text URL.
- Does the trigger fire on **Reels**? Undocumented. Test on a Reel specifically, since
  Reels are likely where most comments come from.
- Are commenters auto-created as contacts, and with what fields?

## Meta platform note (not from GHL's KB)
Instagram's Private Replies allow **one** private message in response to a comment,
which opens the standard 24-hour messaging window. After 24 hours with no reply from
the user, you generally cannot DM them again. This means a "wait 3 days then follow up"
step will likely fail unless the person replied. Verify against Meta's current
Messenger Platform policy before relying on any delayed follow-up.
