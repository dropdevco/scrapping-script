"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { Lang } from "@/lib/types";
import { getDict, type Dict } from "@/lib/i18n";

const LangContext = createContext<{ lang: Lang; t: Dict }>({
  lang: "en",
  t: getDict("en"),
});

export function LangProvider({ lang, children }: { lang: Lang; children: React.ReactNode }) {
  return (
    <LangContext.Provider value={{ lang, t: getDict(lang) }}>{children}</LangContext.Provider>
  );
}

export function useLang() {
  return useContext(LangContext);
}

export function LangToggle() {
  const { lang } = useLang();
  const router = useRouter();
  const [pendingLang, setPendingLang] = useState<Lang | null>(null);
  const switchingLang = pendingLang && pendingLang !== lang ? pendingLang : null;
  const activeLang = switchingLang ?? lang;

  useEffect(() => {
    if (!pendingLang) return;
    if (pendingLang === lang) return;
    document.cookie = `lang=${pendingLang};path=/;max-age=31536000;samesite=lax`;
    router.refresh();
  }, [lang, pendingLang, router]);

  return (
    <div
      className="relative grid w-[4.6rem] grid-cols-2 overflow-hidden rounded-full border-[1.5px] border-ink bg-card p-0.5 font-condensed text-[11px] font-semibold tracking-wide shadow-[2px_2px_0_rgba(20,17,24,0.16)]"
      aria-label="Language"
    >
      <span
        aria-hidden
        className={`absolute bottom-0.5 left-0 top-0.5 w-[calc(50%-0.125rem)] rounded-full bg-ink shadow-[0_0_0_1px_rgba(255,255,255,0.08)_inset] transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] ${
          activeLang === "es" ? "translate-x-[calc(100%+0.125rem)]" : "translate-x-0.5"
        }`}
      />
      {(["en", "es"] as const).map((l) => (
        <button
          key={l}
          onClick={() => {
            if (l !== activeLang) setPendingLang(l);
          }}
          className={`relative z-10 rounded-full px-2.5 py-1 uppercase transition-[color,transform] duration-200 ease-[cubic-bezier(0.32,0.72,0,1)] ${
            activeLang === l ? "text-paper" : "text-ink-soft hover:text-ink hover:-translate-y-px"
          }`}
          aria-pressed={activeLang === l}
          disabled={switchingLang === l}
        >
          {l}
        </button>
      ))}
    </div>
  );
}
