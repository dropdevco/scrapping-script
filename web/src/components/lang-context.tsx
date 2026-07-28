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

  useEffect(() => {
    if (!pendingLang) return;
    document.cookie = `lang=${pendingLang};path=/;max-age=31536000;samesite=lax`;
    router.refresh();
  }, [pendingLang, router]);

  return (
    <div className="flex items-center rounded-full border-[1.5px] border-ink bg-card p-0.5 font-condensed text-[11px] font-semibold tracking-wide">
      {(["en", "es"] as const).map((l) => (
        <button
          key={l}
          onClick={() => setPendingLang(l)}
          className={`rounded-full px-2.5 py-1 uppercase transition-colors duration-200 ease-[cubic-bezier(0.32,0.72,0,1)] ${
            lang === l ? "bg-ink text-paper" : "text-ink-soft hover:text-ink"
          }`}
          aria-pressed={lang === l}
        >
          {l}
        </button>
      ))}
    </div>
  );
}
