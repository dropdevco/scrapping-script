"use client";

import { useEffect, useState } from "react";
import { motion, useScroll, useTransform, useReducedMotion, type MotionValue } from "motion/react";

/*
  Site-wide cut-paper collage of real El Paso + Juárez landmark photographs
  (/public/landmarks). A single FIXED viewport layer behind all content
  (negative z, pointer-events-none) so it never affects document flow or the
  Suspense boundaries. This is the ONLY landmark collage on the site — section
  -scoped copies were removed because two layers sharing the same photos always
  collided with each other.

  Nothing renders until the user has scrolled past #hero-block (measured live
  via ResizeObserver, so this keeps working when the hero is redesigned) — no
  imagery behind the hero, ever.

  ── Guaranteed non-overlap ─────────────────────────────────────────────────
  Pieces are laid out in vertical LANES spanning the full viewport width, not
  pinned to the left/right corners. Two rules make collisions impossible at any
  scroll position:

    1. Pieces in DIFFERENT lanes never overlap horizontally. Each lane is
       100/lanes vw wide and every piece is sized so that its rotated bounding
       width (w·cosθ + h·sinθ, θ ≤ MAX_ROTATE) stays well inside its lane.
    2. Pieces in the SAME lane share ONE parallax factor, so their vertical
       spacing is constant forever — scrolling can never close the gap. That
       gap (STRIDE) is larger than the tallest rotated piece can ever be.

  Because lanes are horizontally disjoint, each lane is free to parallax at its
  own rate without any cross-lane collision risk.

  Photos are cropped into a fixed aspect box and clipped with a jagged "torn
  edge" mask so they read as physically torn magazine clippings pasted onto
  the paper rather than plain rectangles.
*/

const L = "/landmarks";

/* Photo pool, drawn round-robin so neighbouring lanes never repeat an image. */
const PHOTOS: { src: string; aspect: string; torn: "a" | "b" }[] = [
  { src: `${L}/elpasostar.jpg`, aspect: "3/2", torn: "a" },
  { src: `${L}/laequis.jpg`, aspect: "4/3", torn: "b" },
  { src: `${L}/muraljuanga.jpg`, aspect: "4/5", torn: "a" },
  { src: `${L}/downtownskyline.jpg`, aspect: "3/2", torn: "b" },
  { src: `${L}/juarezcategral.jpg`, aspect: "16/9", torn: "a" },
  { src: `${L}/plazatheatre.jpg`, aspect: "2/3", torn: "b" },
  { src: `${L}/benitojuarez.jpg`, aspect: "4/5", torn: "a" },
  { src: `${L}/sanjacinto.jpg`, aspect: "3/2", torn: "b" },
  { src: `${L}/elpasodowntown.jpg`, aspect: "3/2", torn: "a" },
];

/* Layout per breakpoint. `width` is deliberately narrower than the lane
   (100/lanes vw) — see rule 1 above. The rem cap keeps pieces sane on ultra
   -wide displays and, because it only ever shrinks them, preserves the
   guarantee. Widening any of these without widening the lane would break
   non-overlap. */
type Layout = { lanes: number; perLane: number; width: string };

const MOBILE: Layout = { lanes: 2, perLane: 3, width: "min(34vw, 15rem)" }; // lane 50vw
const TABLET: Layout = { lanes: 3, perLane: 2, width: "min(23vw, 17rem)" }; // lane 33.3vw
const DESKTOP: Layout = { lanes: 4, perLane: 2, width: "min(17vw, 20rem)" }; // lane 25vw

const MAX_ROTATE = 4; // degrees — factored into the lane-width budget
const BASE_TOP = 6; // vh, first piece in lane 0
const STRIDE = 95; // vh between pieces WITHIN a lane (> tallest rotated piece)
const STAGGER = 19; // vh each lane is pushed down, so lanes read diagonally

type Placed = {
  src: string;
  aspect: string;
  torn: "a" | "b";
  leftPct: number;
  top: number; // vh past the hero gate
  rotate: number;
  factor: number;
  opacity: number;
};

function buildLayout({ lanes, perLane, width }: Layout): { pieces: Placed[]; width: string } {
  const pieces: Placed[] = [];

  for (let lane = 0; lane < lanes; lane++) {
    // One factor per lane — this is what freezes intra-lane spacing.
    const factor = 0.3 + lane * 0.09;

    for (let j = 0; j < perLane; j++) {
      const n = lane * perLane + j;
      const photo = PHOTOS[n % PHOTOS.length];
      pieces.push({
        ...photo,
        leftPct: ((lane + 0.5) / lanes) * 100,
        top: BASE_TOP + j * STRIDE + lane * STAGGER,
        // Alternating tilt, always within the rotation budget.
        rotate: (n % 2 === 0 ? 1 : -1) * (2 + (n % 3)) * (MAX_ROTATE / 4),
        factor,
        opacity: 0.88 - j * 0.05,
      });
    }
  }

  return { pieces, width };
}

/* Height (px) of #hero-block, kept live via ResizeObserver so this adapts
   automatically when the hero is redesigned. */
function useGateOffset(): number {
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    const el = document.getElementById("hero-block");
    if (!el) return;
    const update = () => setOffset(el.getBoundingClientRect().height);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return offset;
}

/* Returns null until mounted so the server and first client render agree
   (this layer is decorative and aria-hidden, so rendering nothing initially
   costs nothing). */
function useLayout(): Layout | null {
  const [layout, setLayout] = useState<Layout | null>(null);

  useEffect(() => {
    const sm = window.matchMedia("(min-width: 640px)");
    const lg = window.matchMedia("(min-width: 1024px)");
    const pick = () => setLayout(lg.matches ? DESKTOP : sm.matches ? TABLET : MOBILE);
    pick();
    sm.addEventListener("change", pick);
    lg.addEventListener("change", pick);
    return () => {
      sm.removeEventListener("change", pick);
      lg.removeEventListener("change", pick);
    };
  }, []);

  return layout;
}

function Cutout({
  piece,
  width,
  scrollY,
  gateOffset,
  reduce,
}: {
  piece: Placed;
  width: string;
  scrollY: MotionValue<number>;
  gateOffset: number;
  reduce: boolean;
}) {
  // Nothing moves until scroll has cleared the hero block; past that, normal
  // parallax proceeds using scroll distance beyond the gate.
  const y = useTransform(scrollY, (v) => {
    if (reduce) return 0;
    const beyond = Math.max(0, v - gateOffset);
    return -beyond * piece.factor;
  });

  return (
    <motion.div
      aria-hidden
      style={{
        y,
        x: "-50%", // centre the piece on its lane
        top: `calc(${piece.top}vh + ${gateOffset}px)`,
        left: `${piece.leftPct}%`,
        width,
        rotate: piece.rotate,
        opacity: piece.opacity,
      }}
      className="paper-shadow absolute"
    >
      <div className={piece.torn === "a" ? "torn-a" : "torn-b"} style={{ aspectRatio: piece.aspect }}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={piece.src}
          alt=""
          draggable={false}
          className="h-full w-full select-none object-cover"
        />
      </div>
    </motion.div>
  );
}

export function LandmarkBackdrop() {
  const { scrollY } = useScroll();
  const reduce = useReducedMotion() ?? false;
  const gateOffset = useGateOffset();
  const layout = useLayout();

  if (!layout) return null;
  const { pieces, width } = buildLayout(layout);

  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      {pieces.map((p, i) => (
        <Cutout
          key={`${p.src}-${i}`}
          piece={p}
          width={width}
          scrollY={scrollY}
          gateOffset={gateOffset}
          reduce={reduce}
        />
      ))}
    </div>
  );
}
