"use client";

import { useRef } from "react";
import {
  motion,
  useScroll,
  useTransform,
  useReducedMotion,
  type MotionValue,
} from "motion/react";

/*
  Paper-cut El Paso horizon behind the hero. Four flat cut-paper layers
  (sky+sun, Franklin Mountains + Lone Star, downtown skyline, foreground
  missions) drift up at different rates on scroll for depth. Transform-only,
  pointer-events-none, and frozen under prefers-reduced-motion.
*/

function Layer({
  y,
  z,
  children,
}: {
  y: MotionValue<number> | number;
  z: number;
  children: React.ReactNode;
}) {
  return (
    <motion.div
      style={{ y, zIndex: z }}
      className="pointer-events-none absolute inset-x-0 bottom-0"
    >
      {children}
    </motion.div>
  );
}

export function ParallaxScene() {
  const ref = useRef<HTMLDivElement>(null);
  const reduce = useReducedMotion();
  const { scrollY } = useScroll();

  // Nearer layers rise faster → parallax depth. Frozen when reduced-motion.
  const ySky = useTransform(scrollY, [0, 800], [0, reduce ? 0 : -30]);
  const yMtn = useTransform(scrollY, [0, 800], [0, reduce ? 0 : -60]);
  const ySky2 = useTransform(scrollY, [0, 800], [0, reduce ? 0 : -95]);
  const yFront = useTransform(scrollY, [0, 800], [0, reduce ? 0 : -140]);

  return (
    <div
      ref={ref}
      aria-hidden
      className="pointer-events-none absolute inset-x-0 bottom-0 h-[72%] overflow-hidden"
    >
      {/* Layer 1 — sky wash + halftone sun */}
      <Layer y={ySky} z={1}>
        <svg viewBox="0 0 1200 420" preserveAspectRatio="xMidYMax slice" className="h-[420px] w-full">
          <defs>
            <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#fbf6ec" stopOpacity="0" />
              <stop offset="1" stopColor="#ffe0b8" />
            </linearGradient>
          </defs>
          <rect width="1200" height="420" fill="url(#sky)" />
          <circle cx="965" cy="150" r="60" fill="var(--color-pop-yellow)" />
          <circle cx="965" cy="150" r="60" className="halftone-red" opacity="0.35" />
        </svg>
      </Layer>

      {/* Layer 2 — Franklin Mountains + Lone Star */}
      <Layer y={yMtn} z={2}>
        <svg viewBox="0 0 1200 420" preserveAspectRatio="xMidYMax slice" className="h-[420px] w-full">
          <path
            className="paper-shadow"
            d="M0 300 L150 190 L250 240 L390 150 L520 210 L680 140 L820 205 L980 158 L1120 220 L1200 190 L1200 420 L0 420 Z"
            fill="#cf9366"
          />
          <path
            className="paper-shadow"
            d="M660 118 l9 26 27 1 -21 18 8 27 -23 -16 -23 16 8 -27 -21 -18 27 -1 Z"
            fill="var(--color-pop-red)"
          />
        </svg>
      </Layer>

      {/* Layer 3 — downtown skyline */}
      <Layer y={ySky2} z={3}>
        <svg viewBox="0 0 1200 420" preserveAspectRatio="xMidYMax slice" className="h-[420px] w-full">
          <g className="paper-shadow" fill="#7a3f66">
            <rect x="90" y="300" width="70" height="120" />
            <rect x="172" y="250" width="52" height="170" />
            <rect x="300" y="230" width="80" height="190" />
            <rect x="300" y="205" width="22" height="25" />
            <rect x="470" y="285" width="60" height="135" />
            <rect x="640" y="255" width="58" height="165" />
            <rect x="740" y="300" width="66" height="120" />
            <rect x="860" y="268" width="52" height="152" />
            <rect x="1010" y="290" width="72" height="130" />
            <rect x="1100" y="312" width="60" height="108" />
          </g>
        </svg>
      </Layer>

      {/* Layer 4 — foreground missions + papel picado */}
      <Layer y={yFront} z={4}>
        <svg viewBox="0 0 1200 420" preserveAspectRatio="xMidYMax slice" className="h-[420px] w-full">
          <g className="paper-shadow" fill="var(--color-ink)">
            {/* adobe mission block */}
            <rect x="120" y="330" width="150" height="90" />
            <path d="M120 330 a75 40 0 0 1 150 0 Z" />
            <rect x="190" y="292" width="6" height="24" />
            <rect x="180" y="300" width="26" height="6" />
            {/* arches */}
            <rect x="900" y="340" width="180" height="80" />
            <path d="M918 420 v-38 a20 20 0 0 1 40 0 v38 Z" fill="var(--color-paper)" />
            <path d="M980 420 v-38 a20 20 0 0 1 40 0 v38 Z" fill="var(--color-paper)" />
            <path d="M1042 420 v-38 a20 20 0 0 1 20 -20 v58 Z" fill="var(--color-paper)" />
          </g>
        </svg>
        {/* papel picado string */}
        <svg viewBox="0 0 1200 46" preserveAspectRatio="none" className="absolute -top-2 left-0 h-6 w-full">
          <line x1="0" y1="6" x2="1200" y2="6" stroke="var(--color-ink)" strokeWidth="1.5" />
          {Array.from({ length: 20 }).map((_, i) => {
            const colors = ["var(--color-cosmo)", "var(--color-pop-blue)", "var(--color-pop-yellow)", "var(--color-pop-red)"];
            const x = i * 60 + 8;
            return (
              <path
                key={i}
                d={`M${x} 6 h44 v20 l-22 12 l-22 -12 Z`}
                fill={colors[i % colors.length]}
              />
            );
          })}
        </svg>
      </Layer>
    </div>
  );
}
