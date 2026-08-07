"""Daily Instagram carousel: "Today in El Paso".

Turns the events already in Supabase into one carousel post per day — slide 1 a
branded cover, slides 2..N one event each (source photo with the title, time and
venue rendered onto it).

Pipeline (see __main__.py for the CLI):

    build   select today's events -> download + re-encode photos -> render
            1080x1350 JPEG slides -> upload to Supabase Storage -> insert an
            ig_posts row as 'draft'
    publish claim an 'approved' row and push it through the Graph API's
            3-step carousel flow
    prune   sweep slide objects older than the retention window

Core invariant: ALL third-party I/O happens at build time. Dead CDN links,
hotlink protection, WebP/PNG/AVIF sources and undersized images are all resolved
while downloading and re-encoding, so by publish time every slide is a JPEG we
own, at exactly the right dimensions, on infrastructure we control. The publisher
can only fail Meta-side.

Corollary: the caption and slides are frozen at build time. The publisher must
never regenerate either, or the human would approve something different from
what actually ships.
"""
