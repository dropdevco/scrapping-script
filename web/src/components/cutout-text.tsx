/*
  Ransom-note / magazine-cutout lettering. Each character is a "clipping":
  a different display font, a colored paper backing, a small rotation and
  vertical jitter, a glued-paper shadow.

  Everything is DETERMINISTIC (seeded by char + index) — no Math.random —
  so server and client render identically (no hydration mismatch). Pure
  function component, safe to render on the server.

  Use sparingly on short, accent-free words (the wordmark, a hero accent) —
  the ransom fonts are loaded latin-only.
*/

const FONTS = [
  "var(--font-anton), sans-serif",
  "var(--font-archivo-black), sans-serif",
  "var(--font-bungee), sans-serif",
  "var(--font-rubik-mono), monospace",
  "var(--font-oswald), sans-serif",
  "var(--font-fraunces), serif",
];

const CHIPS = [
  { bg: "var(--color-cosmo)", ink: "#ffffff" },
  { bg: "var(--color-pop-yellow)", ink: "var(--color-ink)" },
  { bg: "var(--color-pop-blue)", ink: "#ffffff" },
  { bg: "var(--color-pop-red)", ink: "#ffffff" },
  { bg: "var(--color-card)", ink: "var(--color-ink)" },
  { bg: "var(--color-ink)", ink: "var(--color-paper)" },
];

function hash(str: string, seed: number): number {
  let x = seed | 0;
  for (let i = 0; i < str.length; i++) x = (Math.imul(x, 31) + str.charCodeAt(i)) | 0;
  return Math.abs(x);
}

export function CutoutText({
  text,
  seed = 7,
  className = "",
}: {
  text: string;
  seed?: number;
  className?: string;
}) {
  return (
    <span className={`inline-flex items-center gap-[0.08em] whitespace-nowrap ${className}`}>
      {[...text].map((ch, i) => {
        if (ch === " ") return <span key={i} className="w-[0.32em]" />;
        const h = hash(ch + i, seed);
        const font = FONTS[h % FONTS.length];
        const chip = CHIPS[(h >> 3) % CHIPS.length];
        const rot = ((h % 13) - 6) * 0.55; // ~±3.3deg, restrained
        const dy = ((h >> 4) % 5) - 2; // -2..2 px vertical jitter
        return (
          <span
            key={i}
            className="paper-shadow-sm inline-block rounded-[0.12em] px-[0.14em] leading-[0.98]"
            style={{
              fontFamily: font,
              background: chip.bg,
              color: chip.ink,
              transform: `rotate(${rot}deg) translateY(${dy}px)`,
            }}
          >
            {ch}
          </span>
        );
      })}
    </span>
  );
}
