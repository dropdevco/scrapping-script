"use client";

import { useCallback, useEffect, useRef, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useLang } from "./lang-context";

/* All filter state lives in the URL — so the desktop aside and the mobile
   drawer are two independent instances of <FilterPanel> that stay in sync
   with zero shared React state. Categories are multi-select (comma-joined). */

function useFilterState() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [, startTransition] = useTransition();

  const setParam = useCallback(
    (key: string, value: string) => {
      const next = new URLSearchParams(params.toString());
      if (value) next.set(key, value);
      else next.delete(key);
      startTransition(() => router.replace(`${pathname}?${next.toString()}`, { scroll: false }));
    },
    [params, pathname, router],
  );

  const selected = (params.get("categories") ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

  const toggleCategory = useCallback(
    (cat: string) => {
      const set = new Set(selected);
      if (set.has(cat)) set.delete(cat);
      else set.add(cat);
      setParam("categories", [...set].join(","));
    },
    [selected, setParam],
  );

  const clearAll = useCallback(() => {
    startTransition(() => router.replace(pathname, { scroll: false }));
  }, [pathname, router]);

  const q = params.get("q") ?? "";
  const city = params.get("city") ?? "";
  const when = params.get("when") ?? "";
  const activeCount = selected.length + (city ? 1 : 0) + (when ? 1 : 0) + (q ? 1 : 0);

  return { q, city, when, selected, setParam, toggleCategory, clearAll, activeCount };
}

function Toggle({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-full border-[1.5px] px-3 py-1.5 text-left text-[13px] transition-all duration-200 ease-[cubic-bezier(0.32,0.72,0,1)] ${
        active
          ? "border-ink bg-cosmo font-semibold text-white shadow-[2px_2px_0_var(--color-ink)]"
          : "border-line bg-card text-ink-soft hover:border-ink hover:text-ink"
      }`}
    >
      {children}
    </button>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-2.5 font-condensed text-[11px] font-semibold uppercase tracking-[0.22em] text-cosmo">
      {children}
    </p>
  );
}

function SearchField({
  initialValue,
  placeholder,
  setParam,
}: {
  initialValue: string;
  placeholder: string;
  setParam: (key: string, value: string) => void;
}) {
  const [search, setSearch] = useState(initialValue);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (debounce.current) clearTimeout(debounce.current);
    },
    [],
  );

  function onSearch(value: string) {
    setSearch(value);
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => setParam("q", value.trim()), 350);
  }

  return (
    <div className="relative">
      <svg
        className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-faint"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
      >
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-3-3" strokeLinecap="round" />
      </svg>
      <input
        value={search}
        onChange={(e) => onSearch(e.target.value)}
        placeholder={placeholder}
        /* 16px on touch layouts is deliberate: iOS Safari auto-zooms the whole
           page when a focused input is under 16px, and never zooms back out.
           The compact 14px is kept for the desktop filter rail. */
        className="w-full rounded-full border-[1.5px] border-ink bg-card py-2.5 pl-10 pr-4 text-[16px] text-ink placeholder:text-ink-faint outline-none transition-shadow duration-200 focus:shadow-[3px_3px_0_var(--color-cosmo)] lg:text-[14px]"
      />
    </div>
  );
}

function FilterPanel({ categories }: { categories: string[] }) {
  const { t } = useLang();
  const { q, city, when, selected, setParam, toggleCategory, clearAll, activeCount } =
    useFilterState();

  const cities = [
    { key: "", label: t.allCities },
    { key: "el paso", label: t.elPaso },
    { key: "juarez", label: t.juarez },
  ];
  const whens = [
    { key: "", label: t.anytime },
    { key: "today", label: t.today },
    { key: "weekend", label: t.thisWeekend },
    { key: "week", label: t.thisWeek },
  ];

  return (
    <div className="flex flex-col gap-6">
      {/* header row */}
      <div className="flex items-center justify-between">
        <h2 className="font-display text-xl font-black italic tracking-tight text-ink">
          {t.filters}
        </h2>
        {activeCount > 0 && (
          <button
            onClick={clearAll}
            className="font-condensed text-[11px] font-semibold uppercase tracking-[0.14em] text-ink-soft underline decoration-cosmo decoration-2 underline-offset-4 transition-colors hover:text-cosmo"
          >
            {t.clearAll} ({activeCount})
          </button>
        )}
      </div>

      {/* search */}
      <SearchField key={q} initialValue={q} placeholder={t.searchPlaceholder} setParam={setParam} />

      {/* cities */}
      <div>
        <SectionLabel>{t.citiesLabel}</SectionLabel>
        <div className="flex flex-wrap gap-2">
          {cities.map((c) => (
            <Toggle key={c.key || "all"} active={city === c.key} onClick={() => setParam("city", c.key)}>
              {c.label}
            </Toggle>
          ))}
        </div>
      </div>

      {/* when */}
      <div>
        <SectionLabel>{t.when}</SectionLabel>
        <div className="flex flex-wrap gap-2">
          {whens.map((w) => (
            <Toggle key={w.key || "any"} active={when === w.key} onClick={() => setParam("when", w.key)}>
              {w.label}
            </Toggle>
          ))}
        </div>
      </div>

      {/* categories — MULTI-select */}
      {categories.length > 0 && (
        <div>
          <SectionLabel>{t.categoriesLabel}</SectionLabel>
          <div className="flex flex-wrap gap-2">
            {categories.map((c) => (
              <Toggle key={c} active={selected.includes(c)} onClick={() => toggleCategory(c)}>
                {c}
              </Toggle>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function Filters({ categories }: { categories: string[] }) {
  const { t } = useLang();
  const { activeCount } = useFilterState();
  const [open, setOpen] = useState(false);

  return (
    <>
      {/* mobile trigger */}
      <button
        onClick={() => setOpen(true)}
        className="mb-6 flex w-full items-center justify-between rounded-full border-[1.5px] border-ink bg-card px-5 py-3 text-sm font-semibold text-ink shadow-[3px_3px_0_var(--color-ink)] lg:hidden"
      >
        <span className="flex items-center gap-2">
          <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75">
            <path d="M3 6h18M6 12h12M10 18h4" strokeLinecap="round" />
          </svg>
          {t.filters}
        </span>
        {activeCount > 0 && (
          <span className="flex h-6 min-w-6 items-center justify-center rounded-full bg-cosmo px-1.5 text-[12px] font-bold text-white">
            {activeCount}
          </span>
        )}
      </button>

      {/* mobile drawer */}
      {open && (
        <div className="fixed inset-0 z-[1200] lg:hidden">
          <div
            className="absolute inset-0 bg-ink/40 backdrop-blur-[2px]"
            onClick={() => setOpen(false)}
          />
          <div className="absolute inset-y-0 left-0 flex w-[86%] max-w-sm flex-col bg-paper shadow-2xl">
            <div className="flex items-center justify-between border-b-[1.5px] border-ink px-5 py-4">
              <span className="font-display text-lg font-black italic">{t.filters}</span>
              <button
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="flex h-8 w-8 items-center justify-center rounded-full border-[1.5px] border-ink bg-card"
              >
                <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M6 6l12 12M18 6L6 18" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <div className="flex-1 overflow-y-auto px-5 py-6">
              <FilterPanel categories={categories} />
            </div>
            <div className="border-t-[1.5px] border-ink p-4">
              <button
                onClick={() => setOpen(false)}
                className="w-full rounded-full bg-ink py-3 text-sm font-semibold text-paper transition-transform duration-200 hover:scale-[1.01]"
              >
                {t.showResults}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* desktop sticky aside */}
      <aside className="sticky top-24 hidden max-h-[calc(100dvh-7rem)] overflow-y-auto rounded-[1.5rem] border-[1.5px] border-ink bg-paper/60 p-6 lg:block">
        <FilterPanel categories={categories} />
      </aside>
    </>
  );
}
