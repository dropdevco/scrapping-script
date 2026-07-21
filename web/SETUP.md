# Chisme Setup Guide

Chisme is a fullstack El Paso + Juárez events discovery site. This guide covers local development setup and deployment.

## Prerequisites

- Node.js 20.9+
- npm
- A Supabase project (free tier: https://supabase.com)
- Google OAuth credentials (for sign-in/event submission)

## 1. Environment Setup

### 1a. Supabase Database

1. Go to your [Supabase project dashboard](https://supabase.com/dashboard)
2. Note your project URL and anon key (from **Settings → API**)
3. Create file `web/.env.local`:
   ```
   NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
   NEXT_PUBLIC_SUPABASE_ANON_KEY=your-anon-key-here
   ```

**Important:** These keys are public (prefixed `NEXT_PUBLIC_`). The anon key has **read-only** permissions via Row-Level Security—no secrets here.

### 1b. Google OAuth

To enable "Sign in with Google" and event submission:

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project or select existing
3. **Enable APIs:**
   - Google+ API (or "OAuth 2.0 API")
4. **Create OAuth 2.0 credentials:**
   - Type: Web application
   - Authorized JavaScript origins: `http://localhost:3000` (dev), `https://your-domain` (production)
   - Authorized redirect URIs: `http://localhost:3000/auth/callback`, `https://your-domain/auth/callback`
   - Copy **Client ID**
5. In Supabase dashboard:
   - Go **Authentication → Providers → Google**
   - Paste Client ID and Client Secret
   - Enable the provider
   - Copy the redirect URL Supabase shows (looks like `https://your-project.supabase.co/auth/v1/callback?provider=google`)
   - Add this to your Google OAuth app's redirect URIs
   - **Important:** The submission form redirects to `/auth/callback?next=/submit` to return users to the form after sign-in

## 2. Local Development

```bash
# Install dependencies (if not done)
npm install

# Start dev server
npm run dev
```

Open http://localhost:3000 in your browser.

### Dev Server Features

- **Hot reload:** Changes to `.tsx`, `.ts`, `.css` reflect instantly
- **Dev overlay:** Errors and warnings appear in-browser
- **Proxy:** Next.js proxy.ts handles Supabase session refresh on every request

## 3. Project Structure

```
web/
├── src/
│   ├── app/                    # Next.js 16 App Router
│   │   ├── page.tsx           # Homepage (event listing)
│   │   ├── layout.tsx         # Root layout (header, footer, provider)
│   │   ├── globals.css        # Design tokens (dark theme)
│   │   ├── error.tsx          # Global error page
│   │   ├── auth/callback/route.ts  # OAuth redirect handler
│   │   ├── events/[id]/page.tsx    # Event detail
│   │   ├── map/page.tsx            # Map view (50+ venues)
│   │   └── submit/page.tsx         # Event submission form
│   ├── components/
│   │   ├── header.tsx         # Nav + language toggle
│   │   ├── auth-button.tsx    # Sign in/out
│   │   ├── event-card.tsx     # Event grid item
│   │   ├── event-map.tsx      # Leaflet map component
│   │   ├── filters.tsx        # Search + time/city/category chips
│   │   ├── submit-form.tsx    # Google OAuth + event form
│   │   ├── lang-context.tsx   # Bilingual (EN/ES) context
│   │   └── map-shell.tsx      # Leaflet dynamic import wrapper
│   └── lib/
│       ├── events.ts          # Event queries (date filters, search)
│       ├── types.ts           # EventRow, Venue, Lang
│       ├── i18n.ts            # Translation dictionary + helpers
│       ├── hash.ts            # SHA-1 for address hashing
│       └── supabase/
│           ├── client.ts      # Browser Supabase client
│           └── server.ts      # Server Supabase client (for SSR)
├── src/proxy.ts                # Next 16 proxy (session refresh)
├── tailwind.config.ts          # Dark theme config
├── next.config.ts              # Turbopack enabled by default
└── package.json
```

## 4. Design System

**Color palette** (dark-first desert theme):
- Night: `#0e0c0a` (page base)
- Surface: `#181410` (cards)
- Sand: `#e8e0d4` (primary text)
- Sunset: `#f4652e` (accent)

**Typography:**
- Display: Bricolage Grotesque (headings)
- Body: Geist Sans (text, UI)

**Spacing & radius:**
- `--radius-shell: 1.75rem` (outer card)
- `--radius-core: 1.375rem` (inner content)

**Animations:**
- Cubic-bezier easing for smooth, intentional motion
- `motion/react` for entry animations (EventCard, EventGrid)

## 5. Key Features

### Event Listing
- Chronologically sorted upcoming events
- Search: title, venue, description
- Filters: city (El Paso / Juárez), time (today / this week), category
- Graceful degradation: failed query shows friendly message, not crash

### Event Detail
- Full event info + image
- Venue name + full address (clickable Google Maps link)
- Category badges, source attribution
- Ticket link (if available)

### Map
- Leaflet (dark CARTO tile layer)
- Pins cluster by venue (shows count if >1 event)
- Popup: venue name, city, event list with dates
- 94 geotagged events across both cities

### Event Submission
- Google OAuth required
- Form: title, description, start/end time, venue, address, city, URL, image
- Venue deduplication via address_hash (reuses existing if found)
- Event status: "pending" (awaits review) → "approved" (visible)

### Bilingual UI
- Toggle: EN ↔ ES (top-right)
- Language persisted via cookie (`lang=es|en`)
- Full i18n dictionary (footer, errors, all labels)
- Locale-aware date/time formatting

## 6. Database Schema (Supabase)

### venues
```sql
id uuid primary key
name text
address text
city text
region text
postal text
country text
lat float (nullable)
lng float (nullable)
address_hash text unique  -- sha1(lower(address) || '|' || lower(name))
created_at timestamp
```

### events
```sql
id uuid primary key
source text               -- "ticketmaster", "events_web", "user_submission"
source_id text (nullable) -- provider's ID
title text
description text (nullable)
start_time timestamp (nullable)
end_time timestamp (nullable)
venue text (nullable)
location text (nullable)  -- formatted full address
url text (nullable)
image_url text (nullable)
categories text[]
status text               -- "approved" or "pending"
venue_id uuid (nullable, FK venues)
submitted_by uuid (nullable, user ID who submitted)
content_hash text unique  -- dedupe key
raw jsonb (nullable)      -- original provider data
first_seen timestamp
last_seen timestamp
```

### Row-Level Security (RLS)

**events** table:
- Anonymous: SELECT approved events only
- Authenticated: SELECT all; INSERT own submissions (status='pending'); UPDATE own submissions
- Admin (future): full access

This allows anyone to browse, logged-in users to submit, and submissions appear after moderation.

## 7. Deployment

### Vercel (Recommended)

1. Push to GitHub (already done)
2. Connect repo to Vercel (vercel.com → Import Project)
3. Set environment variables:
   ```
   NEXT_PUBLIC_SUPABASE_URL=...
   NEXT_PUBLIC_SUPABASE_ANON_KEY=...
   ```
4. Deploy (automatic on git push)

### Other Platforms

- **Netlify:** `npm run build` → `.next` output, serverless functions supported
- **Docker:** `npm run build` && `npm start` (requires Node runtime)

## 8. Data Refresh

The MCP server (`src/scraper/mcp_server.py` from the parent repo) populates Supabase on a schedule via GitHub Actions. To manually refresh:

```bash
cd ..  # back to repo root
PYTHONPATH=src .venv/Scripts/python -c "
import asyncio
from scraper import mcp_server as m
result = asyncio.run(m.search_events(location='El Paso', force_refresh=True))
print(f'{result[\"count\"]} events upserted')
"
```

## 9. Troubleshooting

### "Could not find a relationship between 'events' and 'venues'"
- The Supabase migration (0002_venues.sql) hasn't been applied yet
- Run it in your Supabase SQL Editor

### Map doesn't show pins
- Venues must have `lat` and `lng` populated
- Events must have `venue_id` linked
- Check the database: `select count(*) from venues where lat is not null`

### Google OAuth says "Redirect URI mismatch"
- Ensure your OAuth app includes the exact redirect URL
- For local: `http://localhost:3000/auth/callback`
- For production: `https://your-domain.com/auth/callback`

### "Sign in" button doesn't respond
- Check browser console for errors
- Verify Supabase URL and anon key are correct in `.env.local`
- Verify Google OAuth is enabled in Supabase Auth → Providers

## 10. Next Steps

### Google Analytics / Monitoring
- Add Vercel Analytics (`npm install @vercel/analytics`)
- Or Google Analytics via gtag

### Notifications / Admin Panel
- Supabase Edge Functions for event moderation webhooks
- Future: admin dashboard to approve submissions

### Social Sharing
- Open Graph meta tags (og:image, og:description)
- Twitter cards for event links

### PWA / Offline
- Service worker for offline listing cache
- Push notifications for new events in your city

---

**Questions?** Check the parent README at `../README.md` for the full scraper setup and MCP integration guide.
