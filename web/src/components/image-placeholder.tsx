const SIZES = {
  card: { text: "text-lg", dot: "h-1.5 w-1.5", gap: "gap-1.5" },
  hero: { text: "text-3xl sm:text-4xl", dot: "h-2.5 w-2.5", gap: "gap-2.5" },
} as const;

/* Shown when an event has no image. Doubles as a subtle brand moment —
   the red dot reads as a "recording/live" indicator, echoing the app's
   gossip-and-buzz framing. */
export function ImagePlaceholder({ variant = "card" }: { variant?: keyof typeof SIZES }) {
  const s = SIZES[variant];

  return (
    <div className="flex h-full w-full items-center justify-center bg-gradient-to-br from-surface-2 to-night">
      <div className={`flex items-center ${s.gap}`}>
        <span className={`font-display ${s.text} font-bold tracking-tight text-sand-faint`}>
          chisme
        </span>
        <span className="relative flex shrink-0">
          <span
            className={`absolute inline-flex ${s.dot} animate-ping rounded-full bg-rose-dusk opacity-75`}
          />
          <span className={`relative inline-flex ${s.dot} rounded-full bg-rose-dusk`} />
        </span>
      </div>
    </div>
  );
}
