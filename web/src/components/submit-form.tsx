"use client";

import { useEffect, useState } from "react";
import type { User } from "@supabase/supabase-js";
import { supabaseBrowser } from "@/lib/supabase/client";
import { sha1Hex, venueAddressHash } from "@/lib/hash";
import { useLang } from "./lang-context";

const CATEGORIES = [
  "Music",
  "Sports",
  "Arts & Theatre",
  "Family",
  "Community",
  "Food & Drink",
  "Tech",
  "Other",
];

const inputCls =
  "w-full rounded-[1.125rem] border border-line bg-surface-2 px-4 py-3 text-[15px] text-sand placeholder:text-sand-faint outline-none transition-colors duration-200 focus:border-sand-faint";
const labelCls = "mb-1.5 block text-[11px] uppercase tracking-[0.16em] text-sand-dim";

export function SubmitForm() {
  const { t } = useLang();
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [errMsg, setErrMsg] = useState<string | null>(null);

  useEffect(() => {
    const supabase = supabaseBrowser();
    supabase.auth.getUser().then(({ data }) => {
      setUser(data.user ?? null);
      setReady(true);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_e, session) =>
      setUser(session?.user ?? null),
    );
    return () => sub.subscription.unsubscribe();
  }, []);

  async function signIn() {
    const supabase = supabaseBrowser();
    await supabase.auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: `${window.location.origin}/auth/callback?next=/submit` },
    });
  }

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!user) return;
    const form = new FormData(e.currentTarget);

    const title = String(form.get("title") ?? "").trim();
    const start = String(form.get("start") ?? "");
    const venueName = String(form.get("venue") ?? "").trim();
    const address = String(form.get("address") ?? "").trim();
    const city = String(form.get("city") ?? "");
    const isOnline = city === "online";

    if (!title || !start || (!isOnline && (!venueName || !address))) {
      setState("error");
      setErrMsg(t.errRequired);
      return;
    }

    setState("busy");
    setErrMsg(null);
    const supabase = supabaseBrowser();

    try {
      let venueId: string | null = null;

      if (!isOnline) {
        const fullAddress = address.includes(city) ? address : `${address}, ${city}`;
        const hash = await venueAddressHash(fullAddress, venueName);

        const { data: existing } = await supabase
          .from("venues")
          .select("id")
          .eq("address_hash", hash)
          .maybeSingle();

        if (existing) {
          venueId = existing.id;
        } else {
          const { data: created, error: vErr } = await supabase
            .from("venues")
            .insert({
              name: venueName,
              address: fullAddress,
              city,
              address_hash: hash,
            })
            .select("id")
            .single();
          if (vErr) throw vErr;
          venueId = created.id;
        }
      }

      const startIso = new Date(start).toISOString();
      const endRaw = String(form.get("end") ?? "");
      const contentHash = await sha1Hex(
        `user|${title.toLowerCase()}|${startIso.slice(0, 10)}|${venueName.toLowerCase()}|${user.id}`,
      );

      const { error: eErr } = await supabase.from("events").insert({
        source: "user_submission",
        title,
        description: String(form.get("description") ?? "").trim() || null,
        start_time: startIso,
        end_time: endRaw ? new Date(endRaw).toISOString() : null,
        venue: isOnline ? null : venueName,
        location: isOnline ? null : `${address}${address.includes(city) ? "" : `, ${city}`}`,
        url: String(form.get("url") ?? "").trim() || null,
        image_url: String(form.get("image") ?? "").trim() || null,
        categories: [String(form.get("category") ?? "Other")],
        content_hash: contentHash,
        venue_id: venueId,
        status: "pending",
        submitted_by: user.id,
      });
      if (eErr) throw eErr;

      setState("done");
    } catch (err) {
      console.error(err);
      setState("error");
      setErrMsg(t.errGeneric);
    }
  }

  if (!ready) return <div className="h-40" aria-hidden />;

  if (!user) {
    return (
      <div className="rounded-[1.75rem] bg-surface p-1.5 ring-1 ring-line/70">
        <div className="flex flex-col items-center gap-5 rounded-[1.375rem] bg-surface-2 px-6 py-14 text-center">
          <p className="max-w-sm text-[15px] text-sand-dim">{t.signInToSubmit}</p>
          <button
            onClick={signIn}
            className="group flex items-center gap-2 rounded-full bg-sand py-1.5 pl-5 pr-1.5 text-sm font-medium text-night transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:scale-[1.02]"
          >
            {t.continueWithGoogle}
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-night text-sand">
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="currentColor">
                <path d="M21.35 11.1h-9.17v2.73h6.51c-.33 3.81-3.5 5.44-6.5 5.44C8.36 19.27 5 16.25 5 12c0-4.1 3.2-7.27 7.2-7.27 3.09 0 4.9 1.97 4.9 1.97L19 4.72S16.56 2 12.1 2C6.42 2 2.03 6.8 2.03 12c0 5.05 4.13 10 10.22 10 5.35 0 9.25-3.67 9.25-9.09 0-1.15-.15-1.81-.15-1.81Z" />
              </svg>
            </span>
          </button>
        </div>
      </div>
    );
  }

  if (state === "done") {
    return (
      <div className="rounded-[1.75rem] bg-surface p-1.5 ring-1 ring-line/70">
        <div className="flex flex-col items-center gap-4 rounded-[1.375rem] bg-surface-2 px-6 py-14 text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-sunset/15 text-sunset">
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <path d="m5 13 4 4L19 7" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          <p className="text-[15px] text-sand">{t.submitted}</p>
          <button
            onClick={() => setState("idle")}
            className="text-[13px] text-sand-dim underline decoration-line underline-offset-4 transition-colors hover:text-sand"
          >
            {t.submitAnother}
          </button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="rounded-[1.75rem] bg-surface p-1.5 ring-1 ring-line/70">
      <div className="grid grid-cols-1 gap-5 rounded-[1.375rem] bg-surface-2/60 p-5 md:grid-cols-2 md:p-7">
        <div className="md:col-span-2">
          <label className={labelCls} htmlFor="title">
            {t.evTitle} <span className="text-sunset">*</span>
          </label>
          <input id="title" name="title" required maxLength={140} className={inputCls} />
        </div>

        <div className="md:col-span-2">
          <label className={labelCls} htmlFor="description">
            {t.evDescription}
          </label>
          <textarea id="description" name="description" rows={4} maxLength={2000} className={inputCls} />
        </div>

        <div>
          <label className={labelCls} htmlFor="start">
            {t.evStart} <span className="text-sunset">*</span>
          </label>
          <input id="start" name="start" type="datetime-local" required className={inputCls} />
        </div>
        <div>
          <label className={labelCls} htmlFor="end">
            {t.evEnd}
          </label>
          <input id="end" name="end" type="datetime-local" className={inputCls} />
        </div>

        <div>
          <label className={labelCls} htmlFor="city">
            {t.evCity} <span className="text-sunset">*</span>
          </label>
          <select id="city" name="city" className={inputCls} defaultValue="El Paso, TX">
            <option value="El Paso, TX">El Paso</option>
            <option value="Juárez, CHH">Juárez</option>
            <option value="online">{t.virtual}</option>
          </select>
        </div>
        <div>
          <label className={labelCls} htmlFor="category">
            {t.evCategory}
          </label>
          <select id="category" name="category" className={inputCls} defaultValue="Community">
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className={labelCls} htmlFor="venue">
            {t.evVenueName} <span className="text-sunset">*</span>
          </label>
          <input id="venue" name="venue" maxLength={120} className={inputCls} />
        </div>
        <div>
          <label className={labelCls} htmlFor="address">
            {t.evAddress} <span className="text-sunset">*</span>
          </label>
          <input id="address" name="address" maxLength={200} className={inputCls} />
        </div>

        <div>
          <label className={labelCls} htmlFor="url">
            {t.evUrl}
          </label>
          <input id="url" name="url" type="url" className={inputCls} />
        </div>
        <div>
          <label className={labelCls} htmlFor="image">
            {t.evImage}
          </label>
          <input id="image" name="image" type="url" className={inputCls} />
        </div>

        {state === "error" && errMsg && (
          <p className="text-[13px] text-rose-dusk md:col-span-2">{errMsg}</p>
        )}

        <div className="flex flex-wrap items-center gap-4 md:col-span-2">
          <button
            type="submit"
            disabled={state === "busy"}
            className="group flex items-center gap-2 rounded-full bg-sand py-1.5 pl-5 pr-1.5 text-sm font-medium text-night transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:scale-[1.02] disabled:opacity-60"
          >
            {state === "busy" ? t.submitting : t.submit}
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-night text-sand transition-transform duration-300 group-hover:translate-x-0.5">
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M5 12h14m-6-6 6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
          </button>
          <span className="text-[12px] text-sand-faint">{t.pendingNote}</span>
        </div>
      </div>
    </form>
  );
}
