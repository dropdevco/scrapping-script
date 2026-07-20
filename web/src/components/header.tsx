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
  ];

  return (
    <header className="sticky top-0 z-[1100] border-b border-line/60 bg-night/80 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 md:px-6">
        <Link href="/" className="group flex items-baseline gap-2">
          <span className="font-display text-xl font-bold tracking-tight text-sand">
            frontera
          </span>
          <span className="h-1.5 w-1.5 rounded-full bg-sunset transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] group-hover:scale-150" />
        </Link>

        <nav className="hidden items-center gap-1 md:flex">
          {nav.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`rounded-full px-3.5 py-1.5 text-sm transition-colors duration-200 ${
                pathname === item.href
                  ? "bg-surface text-sand"
                  : "text-sand-dim hover:text-sand"
              }`}
            >
              {item.label}
            </Link>
          ))}
          <Link
            href="/submit"
            className={`rounded-full px-3.5 py-1.5 text-sm transition-colors duration-200 ${
              pathname === "/submit" ? "bg-surface text-sand" : "text-sand-dim hover:text-sand"
            }`}
          >
            {t.submitEvent}
          </Link>
        </nav>

        <div className="flex items-center gap-2.5">
          <LangToggle />
          <AuthButton />
        </div>
      </div>

      {/* mobile nav */}
      <nav className="flex items-center gap-1 overflow-x-auto px-4 pb-2.5 md:hidden">
        {[...nav, { href: "/submit", label: t.submitEvent }].map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={`whitespace-nowrap rounded-full px-3 py-1 text-[13px] transition-colors ${
              pathname === item.href ? "bg-surface text-sand" : "text-sand-dim"
            }`}
          >
            {item.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
