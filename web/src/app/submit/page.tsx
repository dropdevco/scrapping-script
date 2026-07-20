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
      <p className="mb-4 inline-block rounded-full border border-line px-3 py-1 text-[10px] uppercase tracking-[0.2em] text-sand-dim">
        {t.submitEvent}
      </p>
      <h1 className="font-display text-4xl font-bold tracking-tight text-sand md:text-5xl">
        {t.submitYourEvent}
      </h1>
      <p className="mt-4 max-w-lg text-[15px] leading-relaxed text-sand-dim">{t.submitIntro}</p>

      <div className="mt-10">
        <SubmitForm />
      </div>
    </div>
  );
}
