"use server";

import { revalidatePath } from "next/cache";
import { approveIgPostRow, rejectIgPostRow } from "@/lib/ig/moderate";
import { verifyReviewToken } from "@/lib/ig/reviewToken";

/* Server Actions are directly callable, so each one re-verifies the token
   itself rather than trusting an id the page already resolved — same
   reasoning as requireAdmin() in ../actions.ts. */

export async function approveIgPostByToken(token: string): Promise<void> {
  const claim = verifyReviewToken(token);
  if (!claim) throw new Error("This link has expired or is invalid.");
  const res = await approveIgPostRow(claim.postId);
  if (!res.ok) throw new Error(res.message);
  revalidatePath("/admin/ig");
  revalidatePath(`/admin/ig/review/${token}`);
}

export async function rejectIgPostByToken(token: string): Promise<void> {
  const claim = verifyReviewToken(token);
  if (!claim) throw new Error("This link has expired or is invalid.");
  const res = await rejectIgPostRow(claim.postId);
  if (!res.ok) throw new Error(res.message);
  revalidatePath("/admin/ig");
  revalidatePath(`/admin/ig/review/${token}`);
}
