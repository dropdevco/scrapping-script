"use client";

import { useLang } from "@/components/lang-context";

export default function AppError({ reset }: { error: Error; reset: () => void }) {
  const { t } = useLang();

  return (
    <div className="mx-auto max-w-6xl px-4 py-32 md:px-6">
      <div className="mx-auto flex max-w-md flex-col items-center gap-5 rounded-[1.5rem] border-[1.5px] border-ink bg-card px-6 py-16 text-center shadow-[4px_5px_0_var(--color-ink)]">
        <span className="font-display text-3xl font-black italic text-ink/70">
          chisme<span className="text-cosmo">.</span>
        </span>
        <p className="max-w-sm text-[15px] text-ink-soft">{t.errGeneric}</p>
        <button
          onClick={reset}
          className="rounded-full bg-ink px-6 py-2.5 text-sm font-semibold text-paper transition-transform duration-200 hover:scale-[1.03]"
        >
          ↻
        </button>
      </div>
    </div>
  );
}
