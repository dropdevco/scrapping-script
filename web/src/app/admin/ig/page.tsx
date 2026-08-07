import Link from "next/link";
import { isAdminEmail } from "@/lib/admin";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { supabaseServer } from "@/lib/supabase/server";
import { approveIgPost, rejectIgPost } from "../actions";
import { PostCard } from "./PostCard";

export const dynamic = "force-dynamic";

const SLIDES_BUCKET = process.env.IG_SLIDES_BUCKET ?? "ig-slides";
const SIGN_TTL = 60 * 60; // preview only — the publisher signs its own, longer URLs

type IgPost = {
  id: string;
  post_date: string;
  status: string;
  slide_paths: string[] | null;
  caption: string | null;
  event_ids: string[] | null;
  error: string | null;
  created_at: string;
};

function Gate({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto max-w-2xl px-4 py-16 text-center">
      <p className="text-ink-soft">{children}</p>
      <Link
        href="/"
        className="mt-3 inline-block underline decoration-cosmo decoration-2 underline-offset-4"
      >
        Back home
      </Link>
    </div>
  );
}

export default async function AdminIgPage() {
  const supabase = await supabaseServer();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) return <Gate>Sign in to view this page.</Gate>;
  if (!isAdminEmail(user.email)) {
    return <Gate>{user.email} is not on the moderation allowlist.</Gate>;
  }

  const admin = supabaseAdmin();
  const { data, error } = await admin
    .from("ig_posts")
    .select("id, post_date, status, slide_paths, caption, event_ids, error, created_at")
    .in("status", ["draft", "approved", "publishing", "failed"])
    .order("post_date", { ascending: false })
    .limit(10);

  if (error) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-16 text-center text-pop-red">{error.message}</div>
    );
  }

  const posts = (data ?? []) as IgPost[];

  // Sign every slide up front so each card can render its strip. Signed URLs
  // are plain unauthenticated GETs, which is also exactly how Meta fetches them.
  const previews = new Map<string, string[]>();
  for (const post of posts) {
    const paths = post.slide_paths ?? [];
    if (!paths.length) continue;
    const { data: signed } = await admin.storage
      .from(SLIDES_BUCKET)
      .createSignedUrls(paths, SIGN_TTL);
    previews.set(
      post.id,
      (signed ?? []).map((s) => s.signedUrl).filter(Boolean) as string[],
    );
  }

  return (
    <div className="mx-auto max-w-5xl px-4 pb-24 pt-10 md:px-6 md:pt-14">
      <h1 className="font-display text-4xl font-black italic tracking-tight text-ink md:text-5xl">
        Instagram queue
      </h1>
      <p className="mt-1.5 text-sm text-ink-soft">
        Review the rendered carousel before it goes out. Approving hands it to the publisher —
        the slides and caption ship exactly as shown here.
      </p>

      <div className="mt-8 flex flex-col gap-6">
        {posts.map((post) => {
          const slides = previews.get(post.id) ?? [];
          const isDraft = post.status === "draft";
          return (
            <PostCard
              key={post.id}
              post={post}
              slides={slides}
              actions={
                isDraft
                  ? {
                      approve: approveIgPost.bind(null, post.id),
                      reject: rejectIgPost.bind(null, post.id),
                    }
                  : null
              }
            />
          );
        })}

        {posts.length === 0 && (
          <div className="rounded-[1.5rem] border-[1.5px] border-dashed border-ink/40 bg-card p-12 text-center text-ink-soft">
            No carousel waiting on review.
          </div>
        )}
      </div>
    </div>
  );
}
