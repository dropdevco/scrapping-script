"use client";

import { useEffect, useRef, useState } from "react";

const PAPER = "#fbf6ec";
const DOT = "#e0d5c0";
const BRUSH_RADIUS = 61;
const STAMP_SPACING = BRUSH_RADIUS * 0.38;
const COMPLETE_THRESHOLD = 0.025;

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

export function HeroScratch({
  hint,
  loadingLabel,
  children,
}: {
  hint: string;
  loadingLabel: string;
  children: React.ReactNode;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const fireworkRef = useRef<HTMLCanvasElement>(null);
  const fireworkFrameRef = useRef(0);
  const completeRef = useRef(false);
  const [scratched, setScratched] = useState(false);
  const [imageReady, setImageReady] = useState(false);
  const [surfaceReady, setSurfaceReady] = useState(false);
  const [complete, setComplete] = useState(false);
  // null until measured on the client, so SSR and first paint agree.
  const [canScratch, setCanScratch] = useState<boolean | null>(null);

  const scratchEnabled = canScratch === true;
  // On touch there is no scratch surface to paint, so the photo alone gates
  // the reveal — derived rather than pushed through setState in an effect.
  const ready = imageReady && (surfaceReady || canScratch === false);

  /*
    The scratch-off is a POINTER-HOVER interaction: it reveals the photo as
    the cursor passes over it, with no click required. That has no touch
    equivalent — a finger drag over the hero is a SCROLL gesture, so the
    browser fires pointercancel and stops delivering pointermove, and the
    surface never gets scratched. Forcing it to work (touch-action: none)
    would be worse: the hero is ~74dvh, so swallowing vertical swipes there
    would trap the user at the top of the page with no way to scroll down.

    So on touch/coarse-pointer devices we skip the scratch layer entirely and
    show the cover photo directly. Phones get the hero the scratching was
    there to reveal; pointer devices still get the interaction.
  */
  useEffect(() => {
    const mq = window.matchMedia("(hover: hover) and (pointer: fine)");
    const pick = () => setCanScratch(mq.matches);
    pick();
    mq.addEventListener("change", pick);
    return () => mq.removeEventListener("change", pick);
  }, []);

  useEffect(() => {
    const image = imageRef.current;
    if (!image) return;
    if (image.complete && image.naturalWidth > 0) {
      if (typeof image.decode === "function") {
        image.decode().catch(() => undefined).finally(() => setImageReady(true));
      } else {
        setImageReady(true);
      }
    }
  }, []);

  useEffect(() => {
    completeRef.current = complete;
  }, [complete]);

  useEffect(
    () => () => {
      cancelAnimationFrame(fireworkFrameRef.current);
    },
    [],
  );

  function launchFireworks() {
    const canvas = fireworkRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const fireworkCtx = ctx;

    const rect = wrap.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    fireworkCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const colors = ["#e6117f", "#ffd21e", "#1e63ff", "#f5232b", "#fbf6ec"];
    const particles: Array<{
      x: number;
      y: number;
      vx: number;
      vy: number;
      life: number;
      age: number;
      color: string;
      size: number;
    }> = [];

    for (let burst = 0; burst < 6; burst += 1) {
      const cx = rect.width * (0.18 + Math.random() * 0.64);
      const cy = rect.height * (0.18 + Math.random() * 0.42);
      for (let i = 0; i < 42; i += 1) {
        const angle = Math.random() * Math.PI * 2;
        const speed = 1.7 + Math.random() * 4.5;
        particles.push({
          x: cx,
          y: cy,
          vx: Math.cos(angle) * speed,
          vy: Math.sin(angle) * speed - 0.7,
          life: 55 + Math.random() * 28,
          age: 0,
          color: colors[(Math.random() * colors.length) | 0],
          size: 2 + Math.random() * 3.5,
        });
      }
    }

    function tick() {
      fireworkCtx.clearRect(0, 0, rect.width, rect.height);
      for (const p of particles) {
        p.age += 1;
        p.x += p.vx;
        p.y += p.vy;
        p.vy += 0.045;
        const alpha = Math.max(0, 1 - p.age / p.life);
        fireworkCtx.globalAlpha = alpha;
        fireworkCtx.fillStyle = p.color;
        fireworkCtx.beginPath();
        fireworkCtx.arc(p.x, p.y, p.size * alpha, 0, Math.PI * 2);
        fireworkCtx.fill();
      }
      fireworkCtx.globalAlpha = 1;
      if (particles.some((p) => p.age < p.life)) {
        fireworkFrameRef.current = requestAnimationFrame(tick);
      } else {
        fireworkCtx.clearRect(0, 0, rect.width, rect.height);
      }
    }

    cancelAnimationFrame(fireworkFrameRef.current);
    fireworkFrameRef.current = requestAnimationFrame(tick);
  }

  useEffect(() => {
    if (!scratchEnabled) return;
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const scratchWrap = wrap;
    const scratchCanvas = canvas;
    const scratchCtx = ctx;

    function paintSurface(w: number, h: number) {
      scratchCtx.globalCompositeOperation = "source-over";
      scratchCtx.fillStyle = PAPER;
      scratchCtx.fillRect(0, 0, w, h);
      scratchCtx.fillStyle = DOT;
      for (let y = 0; y < h; y += 22) {
        for (let x = 0; x < w; x += 22) {
          scratchCtx.beginPath();
          scratchCtx.arc(x, y, 0.7, 0, Math.PI * 2);
          scratchCtx.fill();
        }
      }
    }

    let raf = 0;
    // Last painted CSS size. Repainting resets scratch progress, so only redo
    // it when the box genuinely changed size.
    let lastW = -1;
    let lastH = -1;

    function resize() {
      const r = scratchWrap.getBoundingClientRect();
      if (!r.width || !r.height) return;
      const w = Math.round(r.width);
      const h = Math.round(r.height);
      if (w === lastW && h === lastH) return;
      lastW = w;
      lastH = h;

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      scratchCanvas.width = Math.round(r.width * dpr);
      scratchCanvas.height = Math.round(r.height * dpr);
      scratchCanvas.style.width = `${r.width}px`;
      scratchCanvas.style.height = `${r.height}px`;
      scratchCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (completeRef.current) {
        scratchCtx.clearRect(0, 0, r.width, r.height);
      } else {
        paintSurface(r.width, r.height);
      }
      setSurfaceReady(true);
    }
    resize();

    let lastX: number | null = null;
    let lastY: number | null = null;
    let firstDone = false;
    let coverageCheck = 0;

    function checkCompletion() {
      coverageCheck = 0;
      if (completeRef.current) return;
      const { width, height } = scratchCanvas;
      if (!width || !height) return;
      const data = scratchCtx.getImageData(0, 0, width, height).data;
      let covered = 0;
      let total = 0;
      const stride = 16;
      for (let i = 3; i < data.length; i += 4 * stride) {
        total += 1;
        if (data[i] > 12) covered += 1;
      }
      if (total > 0 && covered / total <= COMPLETE_THRESHOLD) {
        completeRef.current = true;
        scratchCtx.clearRect(0, 0, width, height);
        setComplete(true);
        launchFireworks();
      }
    }

    function scheduleCompletionCheck() {
      if (coverageCheck || completeRef.current) return;
      coverageCheck = window.setTimeout(checkCompletion, 220);
    }

    function scratchAt(clientX: number, clientY: number) {
      const r = scratchCanvas.getBoundingClientRect();
      const x = clientX - r.left;
      const y = clientY - r.top;
      if (x < 0 || y < 0 || x > r.width || y > r.height) {
        lastX = lastY = null;
        return;
      }
      scratchCtx.globalCompositeOperation = "destination-out";
      scratchCtx.strokeStyle = "rgba(0,0,0,1)";
      scratchCtx.fillStyle = "rgba(0,0,0,1)";
      scratchCtx.lineCap = "round";
      scratchCtx.lineJoin = "round";

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
          scratchCtx,
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
      scheduleCompletionCheck();
    }

    function onMove(e: PointerEvent) {
      // Ignore touch even on hybrid (touch + mouse) machines: a finger drag
      // here is the user trying to scroll, not to scratch.
      if (e.pointerType === "touch") return;
      scratchAt(e.clientX, e.clientY);
    }
    function onLeave() {
      lastX = lastY = null;
    }
    function onResize() {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(resize);
    }

    scratchWrap.addEventListener("pointermove", onMove);
    scratchWrap.addEventListener("pointerleave", onLeave);
    // Observe the WRAPPER, not just window resize: the canvas is sized from
    // the wrapper's box, which also changes when a scrollbar appears, when
    // fonts finish loading, or when streamed content reflows the hero. A
    // window-only listener misses those and leaves the canvas smaller than
    // the photo it covers — the photo then shows through along the edge.
    const ro = new ResizeObserver(onResize);
    ro.observe(scratchWrap);
    window.addEventListener("resize", onResize);

    return () => {
      cancelAnimationFrame(raf);
      if (coverageCheck) window.clearTimeout(coverageCheck);
      ro.disconnect();
      scratchWrap.removeEventListener("pointermove", onMove);
      scratchWrap.removeEventListener("pointerleave", onLeave);
      window.removeEventListener("resize", onResize);
    };
  }, [scratchEnabled]);

  return (
    <div ref={wrapRef} className="relative isolate overflow-hidden">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        ref={imageRef}
        src="/background.webp"
        alt=""
        aria-hidden
        onLoad={() => setImageReady(true)}
        className={`absolute inset-0 -z-10 h-full w-full select-none object-cover transition-opacity duration-500 ${
          ready ? "opacity-100" : "opacity-0"
        }`}
        draggable={false}
      />

      {scratchEnabled && (
        <>
          <canvas ref={canvasRef} aria-hidden className="absolute inset-0 z-0" />
          <canvas
            ref={fireworkRef}
            aria-hidden
            className="pointer-events-none absolute inset-0 z-30"
          />
        </>
      )}

      <div
        className={`pointer-events-none relative z-10 transition-opacity duration-500 ${
          ready ? "visible opacity-100" : "invisible opacity-0"
        }`}
      >
        {children}
      </div>

      <div
        aria-hidden={ready}
        className={`pointer-events-none absolute inset-0 z-40 flex items-center justify-center bg-paper transition-opacity duration-500 ${
          ready ? "opacity-0" : "opacity-100"
        }`}
      >
        <span className="flex items-center gap-3 rounded-full border-[1.5px] border-ink bg-card px-5 py-2 font-condensed text-[11px] font-semibold uppercase tracking-[0.16em] text-ink shadow-[3px_3px_0_var(--color-ink)]">
          <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-cosmo" />
          {loadingLabel}
        </span>
      </div>

      <div
        aria-hidden
        className={`pointer-events-none absolute inset-x-0 bottom-6 z-20 flex justify-center transition-opacity duration-500 ${
          scratched || !ready || !scratchEnabled ? "opacity-0" : "opacity-100"
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
