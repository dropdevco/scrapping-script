"use client";

import { useEffect, useState } from "react";
import { motion, useScroll, useTransform, useReducedMotion, type MotionValue } from "motion/react";

/*
  Site-wide cut-paper collage of real El Paso + Juárez landmark photographs
  (/public/landmarks). A single FIXED viewport layer behind all content
  (negative z, pointer-events-none) so it never affects document flow or the
  Suspense boundaries.

  Nothing renders until the user has scrolled past #hero-block (measured
  live via ResizeObserver, so this keeps working correctly once the hero is
  redesigned) — the hero is a clean placeholder for now, no imagery behind
  it. Past that point, cutouts are spread across ~4 screens of virtual
  space; each drifts upward at its own rate on scroll (parallax), so
  landmarks keep entering from below as you go down the page. Frozen under
  prefers-reduced-motion.

  Photos are real images (not drawn), cropped into a fixed aspect box and
  clipped with a jagged "torn edge" mask so they read as physically torn
  magazine clippings pasted onto the paper, rather than plain rectangles.
*/

type Piece = {
  src: string;
  top: string; // % of viewport height, offset past the hero-block gate
  left?: string;
  right?: string;
  w: string; // responsive width classes
  aspect: string; // "3/2" etc
  torn: "a" | "b";
  rotate?: number;
  factor: number; // parallax speed (px moved per px scrolled, once past the gate)
  opacity?: number;
};

const L = "/landmarks";

const PIECES: Piece[] = [
  // El Paso: the Star on the Mountain
  { src: `${L}/elpasostar.jpg`, top: "2%", right: "-2%", w: "w-60 sm:w-80 lg:w-[26rem]", aspect: "3/2", torn: "a", rotate: -3, factor: 0.28, opacity: 0.92 },
  // Juárez: La Equis
  { src: `${L}/laequis.jpg`, top: "20%", left: "-3%", w: "w-40 sm:w-56 lg:w-72", aspect: "4/3", torn: "b", rotate: 5, factor: 0.55, opacity: 0.88 },
  // Juárez: mural
  { src: `${L}/muraljuanga.jpg`, top: "42%", right: "1%", w: "w-32 sm:w-44 lg:w-56", aspect: "4/5", torn: "a", rotate: -4, factor: 0.42, opacity: 0.88 },
  // El Paso: downtown at night
  { src: `${L}/downtownskyline.jpg`, top: "64%", left: "-4%", w: "w-48 sm:w-64 lg:w-80", aspect: "3/2", torn: "b", rotate: 2, factor: 0.5, opacity: 0.85 },
  // Juárez: Cathedral
  { src: `${L}/juarezcategral.jpg`, top: "88%", right: "-3%", w: "w-48 sm:w-64 lg:w-80", aspect: "16/9", torn: "a", rotate: -2, factor: 0.45, opacity: 0.85 },
  // El Paso: Plaza Theatre
  { src: `${L}/plazatheatre.jpg`, top: "112%", left: "2%", w: "w-32 sm:w-40 lg:w-52", aspect: "2/3", torn: "b", rotate: 4, factor: 0.6, opacity: 0.85 },
  // Juárez: Monumento a Benito Juárez
  { src: `${L}/benitojuarez.jpg`, top: "136%", right: "4%", w: "w-28 sm:w-36 lg:w-48", aspect: "4/5", torn: "a", rotate: -5, factor: 0.5, opacity: 0.82 },
  // El Paso: San Jacinto Plaza
  { src: `${L}/sanjacinto.jpg`, top: "160%", left: "-2%", w: "w-40 sm:w-52 lg:w-64", aspect: "3/2", torn: "b", rotate: 3, factor: 0.42, opacity: 0.82 },
  // El Paso: downtown (second angle)
  { src: `${L}/elpasodowntown.jpg`, top: "184%", right: "-2%", w: "w-44 sm:w-56 lg:w-72", aspect: "3/2", torn: "a", rotate: -2, factor: 0.48, opacity: 0.8 },
  // repeats, smaller, for continuity further down the page
  { src: `${L}/elpasostar.jpg`, top: "208%", left: "-3%", w: "w-32 sm:w-40 lg:w-52", aspect: "3/2", torn: "b", rotate: 2, factor: 0.5, opacity: 0.75 },
  { src: `${L}/laequis.jpg`, top: "232%", right: "2%", w: "w-24 sm:w-32 lg:w-40", aspect: "4/3", torn: "a", rotate: -4, factor: 0.55, opacity: 0.75 },
];

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

function Cutout({
  piece,
  scrollY,
  gateOffset,
  reduce,
}: {
  piece: Piece;
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
        top: `calc(${piece.top} + ${gateOffset}px)`,
        left: piece.left,
        right: piece.right,
        rotate: piece.rotate ?? 0,
        opacity: piece.opacity ?? 0.8,
      }}
      className={`paper-shadow absolute ${piece.w}`}
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

  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      {PIECES.map((p, i) => (
        <Cutout key={i} piece={p} scrollY={scrollY} gateOffset={gateOffset} reduce={reduce} />
      ))}
    </div>
  );
}
