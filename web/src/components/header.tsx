"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LangToggle, useLang } from "./lang-context";
import { AuthButton } from "./auth-button";

export function Header() {
  const { t } = useLang();
  const pathname = usePathname();

  const nav = [
    { href: "/", label: t.upcoming },
    { href: "/map", label: t.map },
    { href: "/submit", label: t.submitEvent },
  ];

  return (
    <header className="sticky top-0 z-[1100] bg-paper/85 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 md:px-6">
        {/* masthead wordmark — Cosmo-style didone */}
        <Link href="/" className="group flex items-baseline gap-0.5">
          <span className="font-display text-2xl font-black italic tracking-tight text-ink">
            chisme
          </span>
          <span className="h-2 w-2 translate-y-[-1px] rounded-full bg-cosmo transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] group-hover:scale-150" />
        </Link>

        {/* nav — condensed small caps */}
        <nav className="hidden items-center gap-6 md:flex">
          {nav.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`font-condensed text-[13px] font-medium uppercase tracking-[0.18em] transition-colors duration-200 ${
                  active
                    ? "text-ink underline decoration-cosmo decoration-[2.5px] underline-offset-[6px]"
                    : "text-ink-soft hover:text-ink"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-2.5">
          <LangToggle />
          <AuthButton />
        </div>
      </div>

      {/* mobile nav — this IS the primary navigation on phones, so the links
          carry vertical padding to give them a real tap target. The text is
          only 12px tall; without it the whole hit area was ~18px. */}
      <nav className="flex items-center gap-3 overflow-x-auto px-4 pb-1 md:hidden">
        {nav.map((item) => {
          const active = pathname === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`whitespace-nowrap px-1 py-2.5 font-condensed text-[12px] font-medium uppercase tracking-[0.16em] transition-colors ${
                active
                  ? "text-ink underline decoration-cosmo decoration-2 underline-offset-4"
                  : "text-ink-soft"
              }`}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>

      {/* masthead rule */}
      <div className="h-[2px] w-full bg-ink" />
    </header>
  );
}
