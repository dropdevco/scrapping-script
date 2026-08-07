/* Shared between /admin/ig (session-authed list) and /admin/ig/review/[token]
   (token-authed single-post page) — one card so the two surfaces can't drift
   visually. `actions` is omitted/null to render read-only (already handled,
   or viewed by someone without write access to it). */

export type IgPostSummary = {
  id: string;
  post_date: string;
  status: string;
  caption: string | null;
  event_ids: string[] | null;
  error: string | null;
};

export type PostCardActions = {
  approve: () => Promise<void>;
  reject: () => Promise<void>;
};

export function PostCard({
  post,
  slides,
  actions,
}: {
  post: IgPostSummary;
  slides: string[];
  actions?: PostCardActions | null;
}) {
  return (
    <article className="rounded-[1.25rem] border-[1.5px] border-ink bg-card p-5 shadow-[3px_3px_0_var(--color-ink)]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-display text-xl font-bold text-ink">
            {new Date(`${post.post_date}T12:00:00`).toLocaleDateString("en-US", {
              weekday: "long",
              month: "long",
              day: "numeric",
            })}
          </h2>
          <p className="mt-1 font-condensed text-[12px] uppercase tracking-[0.14em] text-ink-faint">
            {post.status} · {slides.length} slide{slides.length === 1 ? "" : "s"} ·{" "}
            {(post.event_ids ?? []).length} event{(post.event_ids ?? []).length === 1 ? "" : "s"}
          </p>
        </div>

        {actions && (
          <div className="flex shrink-0 gap-2">
            <form action={actions.approve}>
              <button
                type="submit"
                className="rounded-full bg-cosmo px-4 py-1.5 text-sm font-semibold text-white shadow-[2px_2px_0_var(--color-ink)] transition-transform duration-200 hover:-translate-y-0.5"
              >
                Approve &amp; publish
              </button>
            </form>
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
