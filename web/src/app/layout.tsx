import type { Metadata } from "next";
import { cookies } from "next/headers";
import { Libre_Franklin, Geist } from "next/font/google";
import type { Lang } from "@/lib/types";
import { LangProvider } from "@/components/lang-context";
import { Header } from "@/components/header";
import { getDict } from "@/lib/i18n";
import "./globals.css";

const headline = Libre_Franklin({
  subsets: ["latin", "latin-ext"],
  weight: ["600", "700", "800", "900"],
  variable: "--font-headline",
  display: "swap",
});

const geist = Geist({
  subsets: ["latin", "latin-ext"],
  variable: "--font-geist",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Chisme — El Paso + Juárez events",
  description:
    "Concerts, ballgames, markets, meetups — every event on both sides of the border, in one place.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);

  return (
    <html lang={lang} className={`${headline.variable} ${geist.variable} antialiased`}>
      <body className="min-h-[100dvh]">
        <LangProvider lang={lang}>
          <Header />
          <main>{children}</main>
          <footer className="mt-32 border-t border-line/60">
            <div className="mx-auto flex max-w-6xl flex-col gap-2 px-4 py-10 text-xs text-sand-faint md:flex-row md:items-center md:justify-between md:px-6">
              <span className="font-display text-sm text-sand-dim">
                chisme<span className="text-sunset">.</span>
              </span>
              <span>{t.footerNote}</span>
            </div>
          </footer>
        </LangProvider>
      </body>
    </html>
  );
}
