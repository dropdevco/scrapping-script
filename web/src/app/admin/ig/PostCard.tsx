/* Shared between /admin/ig (session-authed list) and /admin/ig/review/[token]
   (token-authed single-post page) — one card so the two surfaces can't drift
   visually. `actions` is omitted/null to render read-only (already handled,
   or viewed by someone without write access to it). */

// Must match scraper.core.config's IG_TIMEZONE default — this is display
// only (the actual scheduling decision happens server-side in Python), but a
// mismatched timezone here would show the admin the wrong hour.
const TZ = "America/Denver";

function formatScheduledFor(iso: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: TZ,
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(iso));
}

export type IgPostSummary = {
  id: string;
  post_date: string;
  status: string;
  caption: string | null;
  event_ids: string[] | null;
  error: string | null;
  scheduled_for?: string | null;
  auto_approve_at?: string | null;
  approved_by?: string | null;
  kind?: string | null;
  slot?: string | null;
};

/* Latest snapshot for a published post. Optional everywhere: a post younger
   than 24h has no row yet, and a metric Meta stopped offering is null. */
export type IgPostMetrics = {
  reach: number | null;
  views: number | null;
  likes: number | null;
  comments: number | null;
  saves: number | null;
  shares: number | null;
  profile_visits: number | null;
  follows: number | null;
  window_label: string;
};

export type PostCardActions = {
  approve: () => Promise<void>;
  reject: () => Promise<void>;
  publishNow?: () => Promise<void>;
};

export function PostCard({
  post,
  slides,
  actions,
  metrics,
}: {
  post: IgPostSummary;
  slides: string[];
  metrics?: IgPostMetrics | null;
  actions?: PostCardActions | null;
}) {
  return (
    <article className="rounded-[1.25rem] border-[1.5px] border-ink bg-card p-5 shadow-[3px_3px_0_var(--color-ink)]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="flex flex-wrap items-center gap-2 font-display text-xl font-bold text-ink">
            {new Date(`${post.post_date}T12:00:00`).toLocaleDateString("en-US", {
              weekday: "long",
              month: "long",
              day: "numeric",
            })}
            {post.kind === "breaking" && (
              <span className="rounded-full bg-pop-red px-2.5 py-0.5 font-condensed text-[11px] uppercase tracking-[0.1em] text-white">
                Breaking
              </span>
            )}
            {post.slot && (
              <span className="rounded-full border border-ink/30 px-2.5 py-0.5 font-condensed text-[11px] uppercase tracking-[0.1em] text-ink-soft">
                {post.slot}
              </span>
            )}
          </h2>
          <p className="mt-1 font-condensed text-[12px] uppercase tracking-[0.14em] text-ink-faint">
            {post.status} · {slides.length} slide{slides.length === 1 ? "" : "s"} ·{" "}
            {(post.event_ids ?? []).length} event{(post.event_ids ?? []).length === 1 ? "" : "s"}
          </p>
          {post.scheduled_for && (
            <p className="mt-1 text-[13px] text-ink-soft">
              {new Date(post.scheduled_for) <= new Date()
                ? "Ready to publish — next sweep will ship it"
                : `Scheduled for ${formatScheduledFor(post.scheduled_for)}`}
            </p>
          )}
          {post.status === "draft" && post.auto_approve_at && (
            <p className="mt-1 text-[13px] font-semibold text-cosmo">
              Posts automatically at {formatScheduledFor(post.auto_approve_at)} unless cancelled
            </p>
          )}
          {post.approved_by === "auto" && (
            <p className="mt-1 text-[13px] text-ink-faint">Approved automatically — nobody objected</p>
          )}
        </div>

        {actions && (
          <div className="flex flex-wrap shrink-0 gap-2">
            <form action={actions.approve}>
              <button
                type="submit"
                className="rounded-full bg-cosmo px-4 py-1.5 text-sm font-semibold text-white shadow-[2px_2px_0_var(--color-ink)] transition-transform duration-200 hover:-translate-y-0.5"
              >
                Approve
              </button>
            </form>
            {actions.publishNow && (
              <form action={actions.publishNow}>
                <button
                  type="submit"
                  className="rounded-full bg-pop-yellow px-4 py-1.5 text-sm font-semibold text-ink shadow-[2px_2px_0_var(--color-ink)] transition-transform duration-200 hover:-translate-y-0.5"
                >
                  Publish now
                </button>
              </form>
            )}
            <form action={actions.reject}>
              <button
                type="submit"
                className="rounded-full border-[1.5px] border-ink bg-paper px-4 py-1.5 text-sm font-semibold text-ink transition-transform duration-200 hover:-translate-y-0.5"
              >
                Reject
              </button>
            </form>
          </div>
        )}
      </div>

      {post.error && (
        <p className="mt-3 rounded-[0.75rem] border border-pop-red/40 bg-pop-red/5 px-3 py-2 text-[13px] text-pop-red">
          {post.error}
        </p>
      )}

      {metrics && (
        <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 rounded-[0.75rem] border border-line bg-card px-3 py-2">
          {(
            [
              ["Reach", metrics.reach],
              ["Views", metrics.views],
              ["Likes", metrics.likes],
              ["Comments", metrics.comments],
              ["Saves", metrics.saves],
              ["Shares", metrics.shares],
              ["Profile visits", metrics.profile_visits],
              ["Follows", metrics.follows],
            ] as const
          ).map(([label, value]) => (
            <div key={label} className="flex items-baseline gap-1.5">
              <dt className="font-condensed text-[11px] uppercase tracking-[0.14em] text-ink-faint">
                {label}
              </dt>
              <dd className="text-[13px] font-semibold text-ink">
                {typeof value === "number" ? value.toLocaleString() : "—"}
              </dd>
            </div>
          ))}
          <span className="ml-auto self-center font-condensed text-[11px] uppercase tracking-[0.14em] text-ink-faint">
            {metrics.window_label}
          </span>
        </dl>
      )}

      {slides.length > 0 && (
        <div className="mt-4 flex snap-x gap-3 overflow-x-auto pb-2">
          {slides.map((url, i) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={url}
              src={url}
              alt={i === 0 ? "Cover slide" : `Slide ${i + 1}`}
              className="h-72 w-auto shrink-0 snap-start rounded-[0.75rem] border border-line"
            />
          ))}
        </div>
      )}

      {post.caption && (
        <details className="mt-3">
          <summary className="cursor-pointer font-condensed text-[12px] uppercase tracking-[0.14em] text-ink-soft">
            Caption ({post.caption.length} chars)
          </summary>
          <pre className="mt-2 whitespace-pre-wrap rounded-[0.75rem] bg-paper-2 p-3 text-[13px] leading-relaxed text-ink/85">
            {post.caption}
          </pre>
        </details>
      )}
    </article>
  );
}
