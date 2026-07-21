const SIZES = {
  card: { text: "text-lg", dot: "h-1.5 w-1.5", gap: "gap-1.5" },
  hero: { text: "text-3xl sm:text-4xl", dot: "h-2.5 w-2.5", gap: "gap-2.5" },
} as const;

/* Shown when an event has no image. Branded "chisme" wordmark + a pulsing
   pink rec dot on cream paper — the on-air/gossip motif. */
export function ImagePlaceholder({ variant = "card" }: { variant?: keyof typeof SIZES }) {
  const s = SIZES[variant];

  return (
    <div className="relative flex h-full w-full items-center justify-center bg-gradient-to-br from-paper-2 to-card">
      <div className="absolute inset-0 halftone-cosmo opacity-[0.12]" />
      <div className={`relative flex items-center ${s.gap}`}>
        <span className={`font-display ${s.text} font-black italic tracking-tight text-ink/70`}>
          chisme
        </span>
        <span className="relative flex shrink-0">
          <span className={`absolute inline-flex ${s.dot} animate-ping rounded-full bg-cosmo opacity-75`} />
          <span className={`relative inline-flex ${s.dot} rounded-full bg-cosmo`} />
        </span>
      </div>
    </div>
  );
}
