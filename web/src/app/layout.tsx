import type { Metadata } from "next";
import { cookies } from "next/headers";
import Link from "next/link";
import {
  Fraunces,
  Archivo,
  Oswald,
  Anton,
  Archivo_Black,
  Bungee,
  Rubik_Mono_One,
} from "next/font/google";
import type { Lang } from "@/lib/types";
import { LangProvider } from "@/components/lang-context";
import { Header } from "@/components/header";
import { LandmarkBackdrop } from "@/components/landmark-backdrop";
import { getDict } from "@/lib/i18n";
import { siteOrigin } from "@/lib/site";
import "./globals.css";

/* Editorial didone — headlines, hero cover-line, event titles. */
const fraunces = Fraunces({
  subsets: ["latin", "latin-ext"],
  weight: ["400", "600", "900"],
  style: ["normal", "italic"],
  variable: "--font-fraunces",
  display: "swap",
});

/* Body / UI grotesque. */
const archivo = Archivo({
  subsets: ["latin", "latin-ext"],
  variable: "--font-archivo",
  display: "swap",
});

/* Condensed — datelines / magazine kickers. */
const oswald = Oswald({
  subsets: ["latin", "latin-ext"],
  variable: "--font-oswald",
  display: "swap",
});

/* Ransom-note set — cut-from-a-magazine letters (CutoutText). Latin only:
   only used on curated, accent-free words (wordmark + hero accent). */
const anton = Anton({ subsets: ["latin"], weight: "400", variable: "--font-anton", display: "swap" });
const archivoBlack = Archivo_Black({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-archivo-black",
  display: "swap",
});
const bungee = Bungee({ subsets: ["latin"], weight: "400", variable: "--font-bungee", display: "swap" });
const rubikMono = Rubik_Mono_One({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-rubik-mono",
  display: "swap",
});

const fontVars = [
  fraunces.variable,
  archivo.variable,
  oswald.variable,
  anton.variable,
  archivoBlack.variable,
  bungee.variable,
  rubikMono.variable,
].join(" ");

export const metadata: Metadata = {
  metadataBase: new URL(siteOrigin()),
  title: "Chisme — El Paso + Juárez events",
  description:
    "Concerts, ballgames, markets, meetups — every event on both sides of the border, in one place.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);

  return (
    <html lang={lang} className={`${fontVars} antialiased`}>
      <body className="min-h-[100dvh]">
        <LangProvider lang={lang}>
          <LandmarkBackdrop />
          <Header />
          <main>{children}</main>

          <footer className="mt-32">
            <div className="bg-ink text-paper">
              <div className="mx-auto flex max-w-6xl flex-col gap-3 px-4 py-10 md:flex-row md:items-center md:justify-between md:px-6">
                <span className="font-display text-2xl font-black italic tracking-tight">
                  chisme<span className="text-cosmo">.</span>
                </span>
                <div className="flex max-w-md flex-col gap-2 text-xs leading-relaxed text-paper/60">
                  <span>{t.footerNote}</span>
                  <Link
                    href="/crawler/events"
                    className="font-condensed uppercase tracking-[0.14em] text-paper/50 transition-colors hover:text-cosmo"
                  >
                    Event index
                  </Link>
                </div>
              </div>
            </div>
          </footer>
        </LangProvider>
      </body>
    </html>
  );
}
