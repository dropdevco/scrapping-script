"use client";

import { useLang } from "@/components/lang-context";

export default function AppError({ reset }: { error: Error; reset: () => void }) {
  const { t } = useLang();

  return (
    <div className="mx-auto max-w-6xl px-4 py-32 md:px-6">
      <div className="rounded-[1.75rem] bg-surface p-1.5 ring-1 ring-line/70">
        <div className="flex flex-col items-center gap-5 rounded-[1.375rem] bg-surface-2 px-6 py-16 text-center">
          <span className="font-display text-4xl font-bold text-line">f.</span>
          <p className="max-w-sm text-[15px] text-sand-dim">{t.errGeneric}</p>
          <button
            onClick={reset}
            className="rounded-full border border-line px-5 py-2 text-sm text-sand-dim transition-colors duration-200 hover:border-sand-faint hover:text-sand"
          >
            ↻
          </button>
        </div>
      </div>
    </div>
  );
}
