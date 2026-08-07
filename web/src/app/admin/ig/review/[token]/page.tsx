import Link from "next/link";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { verifyReviewToken } from "@/lib/ig/reviewToken";
import { PostCard, type IgPostSummary } from "../../PostCard";
import {
  approveIgPostByToken,
  publishIgPostNowByToken,
  rejectIgPostByToken,
  rescheduleIgPostByToken,
} from "../actions";
import { RescheduleForm } from "../RescheduleForm";

export const dynamic = "force-dynamic";

const SLIDES_BUCKET = process.env.IG_SLIDES_BUCKET ?? "ig-slides";
const SIGN_TTL = 60 * 60;

function Notice({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto max-w-2xl px-4 py-16 text-center">
      <p className="text-ink-soft">{children}</p>
      <Link
        href="/admin/ig"
        className="mt-3 inline-block underline decoration-cosmo decoration-2 underline-offset-4"
      >
        Go to the admin queue
      </Link>
    </div>
  );
}

export default async function ReviewByTokenPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  const claim = verifyReviewToken(token);
  if (!claim) return <Notice>This link has expired or is invalid — sign in at /admin/ig instead.</Notice>;

  const admin = supabaseAdmin();
  const { data, error } = await admin
    .from("ig_posts")
    .select("id, post_date, status, slide_paths, caption, event_ids, error, scheduled_for")
    .eq("id", claim.postId)
    .maybeSingle();

  if (error || !data) return <Notice>Couldn&apos;t find that post.</Notice>;
  const post = data as IgPostSummary & { slide_paths: string[] | null };
  if (post.status !== "draft") {
    return <Notice>Already handled — status: {post.status}.</Notice>;
  }

  const paths = post.slide_paths ?? [];
  let slides: string[] = [];
  if (paths.length) {
    const { data: signed } = await admin.storage
      .from(SLIDES_BUCKET)
      .createSignedUrls(paths, SIGN_TTL);
    slides = (signed ?? []).map((s) => s.signedUrl).filter(Boolean) as string[];
  }

  return (
    <div className="mx-auto max-w-2xl px-4 pb-24 pt-10 md:px-6 md:pt-14">
      <h1 className="font-display text-4xl font-black italic tracking-tight text-ink md:text-5xl">
        Today&apos;s post
      </h1>
      <p className="mt-1.5 text-sm text-ink-soft">
        Review the rendered carousel below. Approving hands it to the publisher — the slides and
        caption ship exactly as shown here.
      </p>
      <div className="mt-8">
        <PostCard
          post={post}
          slides={slides}
          actions={{
            approve: approveIgPostByToken.bind(null, token),
            reject: rejectIgPostByToken.bind(null, token),
            publishNow: publishIgPostNowByToken.bind(null, token),
          }}
        />
        {post.scheduled_for && (
          <RescheduleForm
            scheduledFor={post.scheduled_for}
            onSave={rescheduleIgPostByToken.bind(null, token)}
          />
        )}
      </div>
    </div>
  );
}
