"use client";

import { createContext, useContext } from "react";
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

  function switchTo(next: Lang) {
    document.cookie = `lang=${next};path=/;max-age=31536000;samesite=lax`;
    router.refresh();
  }

  return (
    <div className="flex items-center rounded-full border border-line bg-surface p-0.5 text-[11px] font-medium tracking-wide">
      {(["en", "es"] as const).map((l) => (
        <button
          key={l}
          onClick={() => switchTo(l)}
          className={`rounded-full px-2.5 py-1 uppercase transition-colors duration-200 ease-[cubic-bezier(0.32,0.72,0,1)] ${
            lang === l ? "bg-sand text-night" : "text-sand-dim hover:text-sand"
          }`}
          aria-pressed={lang === l}
        >
          {l}
        </button>
      ))}
    </div>
  );
}
