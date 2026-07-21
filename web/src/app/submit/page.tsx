import { cookies } from "next/headers";
import { getDict } from "@/lib/i18n";
import type { Lang } from "@/lib/types";
import { SubmitForm } from "@/components/submit-form";

export const dynamic = "force-dynamic";

export default async function SubmitPage() {
  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);

  return (
    <div className="mx-auto max-w-3xl px-4 pb-24 pt-12 md:px-6 md:pt-20">
      <p className="mb-4 inline-flex items-center gap-2 font-condensed text-[11px] font-semibold uppercase tracking-[0.28em] text-ink-soft">
        <span className="h-2.5 w-2.5 bg-cosmo" />
        {t.submitEvent}
      </p>
      <h1 className="font-display text-5xl font-black italic tracking-tight text-ink md:text-6xl">
        {t.submitYourEvent}
      </h1>
      <p className="mt-4 max-w-lg text-[15px] leading-relaxed text-ink-soft">{t.submitIntro}</p>

      <div className="mt-10">
        <SubmitForm />
      </div>
    </div>
  );
}
