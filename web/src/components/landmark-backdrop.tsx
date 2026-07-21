"use client";

import { motion, useScroll, useTransform, useReducedMotion, type MotionValue } from "motion/react";

/*
  Site-wide cut-paper collage of El Paso + Juárez landmarks (real .svg assets in
  /public/landmarks). A single FIXED viewport layer behind all content (negative
  z, pointer-events-none) so it never affects document flow or the Suspense
  boundaries. Cutouts are spread across ~3 screens of virtual space; on scroll
  each drifts upward at its own rate (parallax), so landmarks keep entering from
  below as you go down the page. Frozen under prefers-reduced-motion.
*/

type Piece = {
  src: string;
  top: string; // % of viewport height; >100% starts below the fold
  left?: string;
  right?: string;
  w: string; // responsive width classes
  rotate?: number;
  factor: number; // parallax speed (px moved per px scrolled)
  opacity?: number;
};

const L = "/landmarks";

const PIECES: Piece[] = [
  { src: `${L}/star-mountain.svg`, top: "-6%", right: "-2%", w: "w-56 sm:w-72 lg:w-96", rotate: -3, factor: 0.28, opacity: 0.9 },
  { src: `${L}/sun.svg`, top: "3%", right: "24%", w: "w-20 sm:w-28 lg:w-36", factor: 0.5, opacity: 0.85 },
  { src: `${L}/la-equis.svg`, top: "17%", left: "-3%", w: "w-20 sm:w-28 lg:w-40", rotate: 5, factor: 0.62, opacity: 0.85 },
  { src: `${L}/papel-picado.svg`, top: "40%", left: "4%", w: "w-72 sm:w-[30rem] lg:w-[40rem]", rotate: -1, factor: 0.35, opacity: 0.9 },
  { src: `${L}/downtown.svg`, top: "74%", left: "-5%", w: "w-44 sm:w-60 lg:w-80", rotate: 2, factor: 0.5, opacity: 0.82 },
  { src: `${L}/cathedral.svg`, top: "98%", right: "-4%", w: "w-40 sm:w-52 lg:w-72", rotate: -2, factor: 0.45, opacity: 0.82 },
  { src: `${L}/agave.svg`, top: "122%", left: "1%", w: "w-16 sm:w-24 lg:w-32", rotate: 4, factor: 0.6, opacity: 0.8 },
  { src: `${L}/bridge.svg`, top: "150%", right: "3%", w: "w-56 sm:w-72 lg:w-96", rotate: -1, factor: 0.4, opacity: 0.8 },
  { src: `${L}/la-equis.svg`, top: "186%", left: "-2%", w: "w-16 sm:w-24 lg:w-32", rotate: -6, factor: 0.62, opacity: 0.8 },
  { src: `${L}/star-mountain.svg`, top: "214%", right: "-3%", w: "w-40 sm:w-56 lg:w-72", rotate: 2, factor: 0.45, opacity: 0.78 },
  { src: `${L}/sun.svg`, top: "244%", left: "5%", w: "w-16 sm:w-20 lg:w-28", factor: 0.5, opacity: 0.78 },
  { src: `${L}/downtown.svg`, top: "274%", right: "2%", w: "w-36 sm:w-48 lg:w-60", rotate: -2, factor: 0.5, opacity: 0.75 },
];

function Cutout({
  piece,
  scrollY,
  reduce,
}: {
  piece: Piece;
  scrollY: MotionValue<number>;
  reduce: boolean;
}) {
  const y = useTransform(scrollY, (v) => (reduce ? 0 : -v * piece.factor));
  return (
    <motion.div
      aria-hidden
      style={{
        y,
        top: piece.top,
        left: piece.left,
        right: piece.right,
        rotate: piece.rotate ?? 0,
        opacity: piece.opacity ?? 0.8,
      }}
      className={`paper-shadow absolute ${piece.w}`}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={piece.src} alt="" draggable={false} className="w-full select-none" />
    </motion.div>
  );
}

export function LandmarkBackdrop() {
  const { scrollY } = useScroll();
  const reduce = useReducedMotion() ?? false;

  return (
    <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 overflow-hidden">
      {PIECES.map((p, i) => (
        <Cutout key={i} piece={p} scrollY={scrollY} reduce={reduce} />
      ))}
    </div>
  );
}
