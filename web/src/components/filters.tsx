"use client";

import { useCallback, useEffect, useRef, useState, useTransition } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useLang } from "./lang-context";

function Chip({
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
      className={`whitespace-nowrap rounded-full px-3.5 py-1.5 text-[13px] transition-all duration-200 ease-[cubic-bezier(0.32,0.72,0,1)] ${
        active
          ? "bg-sand text-night font-medium"
          : "bg-surface text-sand-dim ring-1 ring-line hover:text-sand hover:ring-sand-faint/60"
      }`}
      aria-pressed={active}
    >
      {children}
    </button>
  );
}

export function Filters({ categories }: { categories: string[] }) {
  const { t } = useLang();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const [, startTransition] = useTransition();

  const q = params.get("q") ?? "";
  const city = params.get("city") ?? "";
  const when = params.get("when") ?? "";
  const category = params.get("category") ?? "";

  const [search, setSearch] = useState(q);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setParam = useCallback(
    (key: string, value: string) => {
      const next = new URLSearchParams(params.toString());
      if (value) next.set(key, value);
      else next.delete(key);
      startTransition(() => {
        router.replace(`${pathname}?${next.toString()}`, { scroll: false });
      });
    },
    [params, pathname, router],
  );

  useEffect(() => setSearch(q), [q]);

  function onSearch(value: string) {
    setSearch(value);
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => setParam("q", value.trim()), 350);
  }

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
    <div className="flex flex-col gap-3.5">
      {/* search */}
      <div className="relative">
        <svg
          className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-sand-faint"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
        >
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3-3" strokeLinecap="round" />
        </svg>
        <input
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder={t.searchPlaceholder}
          className="w-full rounded-full border border-line bg-surface py-3 pl-11 pr-4 text-[15px] text-sand placeholder:text-sand-faint outline-none transition-colors duration-200 focus:border-sand-faint"
        />
      </div>

      {/* chips */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-2">
        {cities.map((c) => (
          <Chip key={c.key || "all"} active={city === c.key} onClick={() => setParam("city", c.key)}>
            {c.label}
          </Chip>
        ))}
        <span className="mx-1 hidden h-4 w-px bg-line md:block" />
        {whens.map((w) => (
          <Chip key={w.key || "any"} active={when === w.key} onClick={() => setParam("when", w.key)}>
            {w.label}
          </Chip>
        ))}
      </div>

      {categories.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <Chip active={category === ""} onClick={() => setParam("category", "")}>
            {t.allCategories}
          </Chip>
          {categories.map((c) => (
            <Chip key={c} active={category === c} onClick={() => setParam("category", c)}>
              {c}
            </Chip>
          ))}
        </div>
      )}
    </div>
  );
}
