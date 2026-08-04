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

/* Document-space top (px) of #events, kept live via ResizeObserver so this
   adapts automatically when the hero is redesigned.

   This component lives in the ROOT LAYOUT, so it survives client-side route
   changes while #events belongs to the page and gets swapped in and out under
   it. Two consequences drive the design here:

     1. The node must be re-resolved when the DOM changes. Binding once on
        mount leaves the observer watching a DETACHED node, and a detached node
        measures as 0 — which drags the gate to the top of the document and
        paints the whole collage straight over the hero.
     2. #events may not exist yet when a route first commits (it streams in
        behind Suspense), so "not found" has to be retried rather than treated
        as final.

   A MutationObserver covers both: it re-attaches when the node appears, is
   replaced, or goes away — no route-change plumbing needed. */
function useEventsGateOffset(): number | null {
  const [offset, setOffset] = useState<number | null>(null);

  useEffect(() => {
    let el: HTMLElement | null = null;
    let ro: ResizeObserver | null = null;

    const update = () => {
      if (!el || !el.isConnected) return; // never measure a detached node
      setOffset(el.getBoundingClientRect().top + window.scrollY);
    };

    const attach = () => {
      const found = document.getElementById("events");
      if (found === el) return;
      el = found;
      ro?.disconnect();
      ro = null;
      if (!el) {
        setOffset(null); // no gate on this route — render nothing
        return;
      }
      ro = new ResizeObserver(update);
      ro.observe(el);
      update();
    };

    attach();
    const mo = new MutationObserver(attach);
    mo.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", update);
    return () => {
      mo.disconnect();
      ro?.disconnect();
      window.removeEventListener("resize", update);
    };
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
  const gateOffset = useEventsGateOffset();
  const layout = useLayout();

  // Belt-and-suspenders hero guard: the pieces' resting position (pre-gate)
  // is a viewport-relative pixel value frozen at `gateOffset`, which is only
  // off-screen as long as gateOffset exceeds the current viewport height.
  // On a short viewport (small browser window, zoomed-in, a shorter hero in
  // another locale) that isn't guaranteed, so also hard-gate on scrollY —
  // nothing paints until the user has actually scrolled past the hero.
  //
  // This is deliberately plain state driven by a scroll listener, NOT a
  // useTransform off the scroll MotionValue: a transform only recomputes when
  // its input fires, so after a client-side route change back to this page
  // (scroll already at 0, nothing to fire) it would keep whatever value it
  // last latched and let the collage paint straight over the hero. Re-running
  // on every gateOffset change makes the gate correct on mount too.
  const [pastGate, setPastGate] = useState(false);

  useEffect(() => {
    if (gateOffset === null) return;
    const check = () => setPastGate(window.scrollY >= gateOffset);
    check();
    window.addEventListener("scroll", check, { passive: true });
    return () => window.removeEventListener("scroll", check);
  }, [gateOffset]);

  if (!layout || gateOffset === null) return null;
  const { pieces, width } = buildLayout(layout);

  return (
    <div
      aria-hidden
      style={{ opacity: pastGate ? 1 : 0, visibility: pastGate ? "visible" : "hidden" }}
      className="pointer-events-none fixed inset-0 -z-10 overflow-hidden"
    >
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
