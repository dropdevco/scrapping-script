"use client";

import { useEffect, useState } from "react";
import type { User } from "@supabase/supabase-js";
import { supabaseBrowser } from "@/lib/supabase/client";
import { sha1Hex, venueAddressHash } from "@/lib/hash";
import { useLang } from "./lang-context";
import { CANONICAL_CATEGORIES } from "@/lib/categories";

const inputCls =
  "w-full rounded-[0.9rem] border-[1.5px] border-ink bg-card px-4 py-3 text-[15px] text-ink placeholder:text-ink-faint outline-none transition-shadow duration-200 focus:shadow-[3px_3px_0_var(--color-cosmo)]";
const labelCls =
  "mb-1.5 block font-condensed text-[11px] font-semibold uppercase tracking-[0.18em] text-cosmo";

export function SubmitForm() {
  const { t } = useLang();
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [categories, setCategories] = useState<string[]>([]);

  function toggleCategory(c: string) {
    setCategories((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]));
  }

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
        categories: categories.length > 0 ? categories : ["Other"],
        content_hash: contentHash,
        venue_id: venueId,
        status: "pending",
        submitted_by: user.id,
      });
      if (eErr) throw eErr;

      setState("done");
      setCategories([]);
    } catch (err) {
      console.error(err);
      setState("error");
      setErrMsg(t.errGeneric);
    }
  }

  if (!ready) return <div className="h-40" aria-hidden />;

  if (!user) {
    return (
      <div className="flex flex-col items-center gap-5 rounded-[1.5rem] border-[1.5px] border-ink bg-card px-6 py-14 text-center shadow-[4px_5px_0_var(--color-ink)]">
        <p className="max-w-sm text-[15px] text-ink-soft">{t.signInToSubmit}</p>
        <button
          onClick={signIn}
          className="group flex items-center gap-2 rounded-full bg-cosmo py-1.5 pl-5 pr-1.5 text-sm font-semibold text-white shadow-[3px_3px_0_var(--color-ink)] transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:-translate-y-0.5"
        >
          {t.continueWithGoogle}
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-ink text-white">
            <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="currentColor">
              <path d="M21.35 11.1h-9.17v2.73h6.51c-.33 3.81-3.5 5.44-6.5 5.44C8.36 19.27 5 16.25 5 12c0-4.1 3.2-7.27 7.2-7.27 3.09 0 4.9 1.97 4.9 1.97L19 4.72S16.56 2 12.1 2C6.42 2 2.03 6.8 2.03 12c0 5.05 4.13 10 10.22 10 5.35 0 9.25-3.67 9.25-9.09 0-1.15-.15-1.81-.15-1.81Z" />
            </svg>
          </span>
        </button>
      </div>
    );
  }

  if (state === "done") {
    return (
      <div className="flex flex-col items-center gap-4 rounded-[1.5rem] border-[1.5px] border-ink bg-card px-6 py-14 text-center shadow-[4px_5px_0_var(--color-cosmo)]">
        <span className="flex h-12 w-12 items-center justify-center rounded-full bg-cosmo text-white shadow-[2px_2px_0_var(--color-ink)]">
          <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <path d="m5 13 4 4L19 7" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
        <p className="text-[15px] text-ink">{t.submitted}</p>
        <button
          onClick={() => setState("idle")}
          className="font-condensed text-[13px] font-medium uppercase tracking-[0.12em] text-ink-soft underline decoration-cosmo decoration-2 underline-offset-4 transition-colors hover:text-cosmo"
        >
          {t.submitAnother}
        </button>
      </div>
    );
  }

  return (
    <form
      onSubmit={onSubmit}
      className="rounded-[1.5rem] border-[1.5px] border-ink bg-card p-5 shadow-[4px_5px_0_var(--color-ink)] md:p-7"
    >
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <div className="md:col-span-2">
          <label className={labelCls} htmlFor="title">
            {t.evTitle} <span className="text-cosmo">*</span>
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
            {t.evStart} <span className="text-cosmo">*</span>
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
            {t.evCity} <span className="text-cosmo">*</span>
          </label>
          <select id="city" name="city" className={inputCls} defaultValue="El Paso, TX">
            <option value="El Paso, TX">El Paso</option>
            <option value="Juárez, CHH">Juárez</option>
            <option value="online">{t.virtual}</option>
          </select>
        </div>
        <div className="md:col-span-2">
          <span className={labelCls}>{t.evCategory}</span>
          <div className="flex flex-wrap gap-2" role="group" aria-label={t.evCategory}>
            {CANONICAL_CATEGORIES.map((c) => {
              const active = categories.includes(c);
              return (
                <button
                  key={c}
                  type="button"
                  aria-pressed={active}
                  onClick={() => toggleCategory(c)}
                  className={`rounded-full border-[1.5px] px-3 py-1.5 text-[13px] transition-all duration-200 ease-[cubic-bezier(0.32,0.72,0,1)] ${
                    active
                      ? "border-ink bg-cosmo font-semibold text-white shadow-[2px_2px_0_var(--color-ink)]"
                      : "border-line bg-card text-ink-soft hover:border-ink hover:text-ink"
                  }`}
                >
                  {c}
                </button>
              );
            })}
          </div>
        </div>

        <div>
          <label className={labelCls} htmlFor="venue">
            {t.evVenueName} <span className="text-cosmo">*</span>
          </label>
          <input id="venue" name="venue" maxLength={120} className={inputCls} />
        </div>
        <div>
          <label className={labelCls} htmlFor="address">
            {t.evAddress} <span className="text-cosmo">*</span>
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
          <p className="text-[13px] font-medium text-pop-red md:col-span-2">{errMsg}</p>
        )}

        <div className="flex flex-wrap items-center gap-4 md:col-span-2">
          <button
            type="submit"
            disabled={state === "busy"}
            className="group flex items-center gap-2 rounded-full bg-cosmo py-1.5 pl-5 pr-1.5 text-sm font-semibold text-white shadow-[3px_3px_0_var(--color-ink)] transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:-translate-y-0.5 disabled:opacity-60"
          >
            {state === "busy" ? t.submitting : t.submit}
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-ink text-white transition-transform duration-300 group-hover:translate-x-0.5">
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M5 12h14m-6-6 6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
          </button>
          <span className="font-condensed text-[12px] uppercase tracking-[0.1em] text-ink-faint">
            {t.pendingNote}
          </span>
        </div>
      </div>
    </form>
  );
}
