"use client";

import { useEffect, useRef, useState } from "react";

/*
  Scratch-to-reveal "scrapbook" hero.

  Layers (back → front):
    1. the photo (/background.webp)
    2. a <canvas> painted the page's beige paper (+ faint dot texture) — moving
       the cursor erases it with destination-out compositing, revealing the
       photo along the path; scrape it all and the whole image shows.
    3. the headline / CTAs (passed as children), on top and always legible.

  The pointer listener lives on the wrapper (not the canvas), and the content
  layer is pointer-events-none (except interactive controls, which opt back in
  via .pointer-events-auto), so you can scrape even "under" the headline while
  buttons stay clickable. No-JS / reduced setups gracefully degrade to a plain
  beige hero (the photo simply stays hidden).
*/

const PAPER = "#fbf6ec";
const DOT = "#e0d5c0";
const BRUSH_RADIUS = 61;
const STAMP_SPACING = BRUSH_RADIUS * 0.38;

function scratchStamp(ctx: CanvasRenderingContext2D, x: number, y: number) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(Math.random() * Math.PI);

  for (let i = 0; i < 11; i += 1) {
    const angle = Math.random() * Math.PI * 2;
    const distance = Math.random() * BRUSH_RADIUS * 0.42;
    const rx = BRUSH_RADIUS * (0.36 + Math.random() * 0.32);
    const ry = BRUSH_RADIUS * (0.18 + Math.random() * 0.26);
    ctx.beginPath();
    ctx.ellipse(
      Math.cos(angle) * distance,
      Math.sin(angle) * distance,
      rx,
      ry,
      Math.random() * Math.PI,
      0,
      Math.PI * 2,
    );
    ctx.fill();
  }

  for (let i = 0; i < 18; i += 1) {
    const angle = Math.random() * Math.PI * 2;
    const inner = BRUSH_RADIUS * (0.28 + Math.random() * 0.54);
    const length = BRUSH_RADIUS * (0.18 + Math.random() * 0.32);
    const sx = Math.cos(angle) * inner;
    const sy = Math.sin(angle) * inner;
    ctx.lineWidth = 2 + Math.random() * 7;
    ctx.beginPath();
    ctx.moveTo(sx, sy);
    ctx.lineTo(sx + Math.cos(angle) * length, sy + Math.sin(angle) * length);
    ctx.stroke();
  }

  ctx.restore();
}

export function HeroScratch({ hint, children }: { hint: string; children: React.ReactNode }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [scratched, setScratched] = useState(false);

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    function paintSurface(w: number, h: number) {
      if (!ctx) return;
      ctx.globalCompositeOperation = "source-over";
      ctx.fillStyle = PAPER;
      ctx.fillRect(0, 0, w, h);
      // faint dot texture to match the rest of the paper page
      ctx.fillStyle = DOT;
      for (let y = 0; y < h; y += 22) {
        for (let x = 0; x < w; x += 22) {
          ctx.beginPath();
          ctx.arc(x, y, 0.7, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }

    let raf = 0;
    function resize() {
      if (!wrap || !canvas || !ctx) return;
      const r = wrap.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(r.width * dpr);
      canvas.height = Math.round(r.height * dpr);
      canvas.style.width = `${r.width}px`;
      canvas.style.height = `${r.height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      paintSurface(r.width, r.height); // NB: repaint resets the scratch on resize
    }
    resize();

    let lastX: number | null = null;
    let lastY: number | null = null;
    let firstDone = false;

    function scratchAt(clientX: number, clientY: number) {
      if (!canvas || !ctx) return;
      const r = canvas.getBoundingClientRect();
      const x = clientX - r.left;
      const y = clientY - r.top;
      if (x < 0 || y < 0 || x > r.width || y > r.height) {
        lastX = lastY = null;
        return;
      }
      ctx.globalCompositeOperation = "destination-out";
      ctx.strokeStyle = "rgba(0,0,0,1)";
      ctx.fillStyle = "rgba(0,0,0,1)";
      ctx.lineCap = "round";
      ctx.lineJoin = "round";

      const fromX = lastX ?? x;
      const fromY = lastY ?? y;
      const dx = x - fromX;
      const dy = y - fromY;
      const distance = Math.hypot(dx, dy);
      const steps = Math.max(1, Math.ceil(distance / STAMP_SPACING));

      for (let i = 0; i <= steps; i += 1) {
        const t = i / steps;
        const jitter = BRUSH_RADIUS * 0.08;
        scratchStamp(
          ctx,
          fromX + dx * t + (Math.random() - 0.5) * jitter,
          fromY + dy * t + (Math.random() - 0.5) * jitter,
        );
      }

      lastX = x;
      lastY = y;
      if (!firstDone) {
        firstDone = true;
        setScratched(true);
      }
    }

    function onMove(e: PointerEvent) {
      scratchAt(e.clientX, e.clientY);
    }
    function onLeave() {
      lastX = lastY = null;
    }
    function onResize() {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(resize);
    }

    wrap.addEventListener("pointermove", onMove);
    wrap.addEventListener("pointerleave", onLeave);
    window.addEventListener("resize", onResize);

    return () => {
      cancelAnimationFrame(raf);
      wrap.removeEventListener("pointermove", onMove);
      wrap.removeEventListener("pointerleave", onLeave);
      window.removeEventListener("resize", onResize);
    };
  }, []);

  return (
    <div ref={wrapRef} className="relative isolate overflow-hidden">
      {/* revealed photo */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/background.webp"
        alt=""
        aria-hidden
        className="absolute inset-0 -z-10 h-full w-full select-none object-cover"
        draggable={false}
      />
      {/* scratch surface — no touch-action override, so mobile can still scroll past */}
      <canvas ref={canvasRef} aria-hidden className="absolute inset-0 z-0" />

      {/* content */}
      <div className="pointer-events-none relative z-10">{children}</div>

      {/* discoverability hint — fades out after the first scrape */}
      <div
        aria-hidden
        className={`pointer-events-none absolute inset-x-0 bottom-6 z-20 flex justify-center transition-opacity duration-500 ${
          scratched ? "opacity-0" : "opacity-100"
        }`}
      >
        <span className="flex items-center gap-2 rounded-full border-[1.5px] border-ink bg-card/90 px-4 py-1.5 font-condensed text-[11px] font-semibold uppercase tracking-[0.16em] text-ink shadow-[2px_2px_0_var(--color-ink)] backdrop-blur-sm">
          <span className="h-2 w-2 animate-ping rounded-full bg-cosmo" />
          {hint}
        </span>
      </div>
    </div>
  );
}
