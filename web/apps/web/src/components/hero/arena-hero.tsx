"use client";

import type { Route } from "next";
import Link from "next/link";
import { useEffect, useRef } from "react";

const REPO_URL = "https://github.com/LingSiewWin/AgoraHack";

const NAV: { label: string; href: string; external?: boolean }[] = [
  { label: "arena", href: "/arena" },
  { label: "colosseum", href: "/colosseum" },
  { label: "console", href: "/console" },
  { label: "docs", href: REPO_URL, external: true },
];

type Tracker = {
  id: number;
  label: string;
  cx: number; cy: number; w: number; h: number;       // smoothed (drawn)
  tcx: number; tcy: number; tw: number; th: number;    // target
  side: number;
  birth: number; seen: number;
  fade: number; grow: number; dying: boolean;
};

type Rect = { cx: number; cy: number; w: number; h: number };

/**
 * Full-screen hero: a looping video with motion-tracked, pixelized boxes that
 * latch onto moving subjects (players/ball), follow them within a shot, and
 * re-acquire on scene cuts. Ported from the standalone canvas prototype.
 */
export function ArenaHero() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const video = videoRef.current;
    if (!canvas || !video) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const CFG = {
      max: 3,
      minDwell: 0.3,
      lostHold: 0.3,
      maxLife: 3.5,
      fadeIn: 0.12, fadeOut: 0.3,
      zoomTime: 0.32,
      follow: 0.2,
      minHalfPx: 40,
      maxHalfFrac: 0.13,
      pad: 1.12,
      cellPx: 16,
      gridAlpha: 0.28,
      boost: 1.12,
      mgW: 48, mgH: 27,
      diffThresh: 20,
      blobThresh: 16,
      minCells: 5,
      maxBlobArea: 0.16,
      maxBlobs: 6,
      cutFloor: 28,
      cutRatio: 2.5,
    };

    const sample = document.createElement("canvas");
    const sctx = sample.getContext("2d")!;
    const mcv = document.createElement("canvas");
    mcv.width = CFG.mgW; mcv.height = CFG.mgH;
    const mctx = mcv.getContext("2d", { willReadFrequently: true })!;

    let W = 0, H = 0, dpr = 1, ready = false, lastT = 0, raf = 0;
    let cover = { scale: 1, ox: 0, oy: 0, dw: 0, dh: 0 };

    const N = CFG.mgW * CFG.mgH;
    let prevGray: Float32Array | null = null;
    const motionSmooth = new Float32Array(N);
    let meanEMA = 0;
    let sceneCut = false;

    const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);
    const boxesOverlap = (a: Rect, b: Rect) =>
      Math.abs(a.cx - b.cx) * 2 < (a.w + b.w) &&
      Math.abs(a.cy - b.cy) * 2 < (a.h + b.h);

    function resize() {
      W = window.innerWidth; H = window.innerHeight;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas!.width = Math.round(W * dpr);
      canvas!.height = Math.round(H * dpr);
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (ready) computeCover();
    }
    function computeCover() {
      const vw = video!.videoWidth, vh = video!.videoHeight;
      if (!vw || !vh) return;
      const s = Math.max(W / vw, H / vh);
      const dw = vw * s, dh = vh * s;
      cover = { scale: s, dw, dh, ox: (W - dw) / 2, oy: (H - dh) / 2 };
    }

    function detectBlobs() {
      mctx.drawImage(video!, 0, 0, CFG.mgW, CFG.mgH);
      const px = mctx.getImageData(0, 0, CFG.mgW, CFG.mgH).data;
      const gray = new Float32Array(N);
      for (let i = 0; i < N; i++) {
        const j = i * 4;
        gray[i] = 0.299 * px[j] + 0.587 * px[j + 1] + 0.114 * px[j + 2];
      }
      if (!prevGray) { prevGray = gray; return [] as Rect[]; }
      const bin = new Uint8Array(N);
      let totalDiff = 0;
      for (let i = 0; i < N; i++) {
        const d = Math.abs(gray[i] - prevGray[i]);
        totalDiff += d;
        motionSmooth[i] = motionSmooth[i] * 0.5 + (d > CFG.diffThresh ? d : 0) * 0.5;
        bin[i] = motionSmooth[i] > CFG.blobThresh ? 1 : 0;
      }
      prevGray = gray;
      const meanDiff = totalDiff / N;
      sceneCut = meanDiff > CFG.cutFloor && meanDiff > CFG.cutRatio * meanEMA;
      meanEMA = meanEMA * 0.9 + meanDiff * 0.1;
      if (sceneCut) { motionSmooth.fill(0); return [] as Rect[]; }

      const gw = CFG.mgW, gh = CFG.mgH;
      const visited = new Uint8Array(N);
      const stack: number[] = [];
      const blobs: { minx: number; miny: number; maxx: number; maxy: number; summ: number; cx: number; cy: number }[] = [];
      for (let i = 0; i < N; i++) {
        if (!bin[i] || visited[i]) continue;
        stack.length = 0; stack.push(i); visited[i] = 1;
        let minx = gw, miny = gh, maxx = 0, maxy = 0, count = 0, summ = 0, sx = 0, sy = 0;
        while (stack.length) {
          const c = stack.pop()!;
          const cx = c % gw, cy = (c / gw) | 0;
          const m = motionSmooth[c];
          count++; summ += m; sx += cx * m; sy += cy * m;
          if (cx < minx) minx = cx; if (cx > maxx) maxx = cx;
          if (cy < miny) miny = cy; if (cy > maxy) maxy = cy;
          for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
            if (!dx && !dy) continue;
            const nx = cx + dx, ny = cy + dy;
            if (nx < 0 || ny < 0 || nx >= gw || ny >= gh) continue;
            const ni = ny * gw + nx;
            if (bin[ni] && !visited[ni]) { visited[ni] = 1; stack.push(ni); }
          }
        }
        const areaFrac = ((maxx - minx + 1) * (maxy - miny + 1)) / N;
        if (count >= CFG.minCells && areaFrac <= CFG.maxBlobArea) {
          blobs.push({ minx, miny, maxx, maxy, summ, cx: sx / summ, cy: sy / summ });
        }
      }
      blobs.sort((a, b) => b.summ - a.summ);
      return blobs.slice(0, CFG.maxBlobs).map(blobToRect);
    }

    function blobToRect(b: { minx: number; miny: number; maxx: number; maxy: number }): Rect {
      const xL = cover.ox + (b.minx / CFG.mgW) * cover.dw;
      const xR = cover.ox + ((b.maxx + 1) / CFG.mgW) * cover.dw;
      const yT = cover.oy + (b.miny / CFG.mgH) * cover.dh;
      const yB = cover.oy + ((b.maxy + 1) / CFG.mgH) * cover.dh;
      const cx = (xL + xR) / 2, cy = (yT + yB) / 2;
      const maxHalf = CFG.maxHalfFrac * Math.min(W, H);
      const hw = Math.min(maxHalf, Math.max(CFG.minHalfPx, ((xR - xL) / 2) * CFG.pad));
      const hh = Math.min(maxHalf, Math.max(CFG.minHalfPx, ((yB - yT) / 2) * CFG.pad));
      return { cx, cy, w: hw * 2, h: hh * 2 };
    }

    let trackers: Tracker[] = [];
    let _id = 0;
    function newTracker(r: Rect, now: number): Tracker {
      _id = (_id + 1) % 100;
      return {
        id: _id, label: "TRK_" + String(_id).padStart(2, "0"),
        cx: r.cx, cy: r.cy, w: r.w, h: r.h,
        tcx: r.cx, tcy: r.cy, tw: r.w, th: r.h,
        side: r.cx < W / 2 ? 1 : -1,
        birth: now, seen: now, fade: 0, grow: 0, dying: false,
      };
    }

    function updateTrackers(now: number, dt: number) {
      const rects = detectBlobs();
      if (sceneCut) {
        for (const t of trackers) t.dying = true;
      } else {
        const used = new Array(rects.length).fill(false);
        for (const t of trackers) {
          if (t.dying) continue;
          let best = -1, bd = Infinity;
          for (let i = 0; i < rects.length; i++) {
            if (used[i]) continue;
            const d = Math.hypot(t.cx - rects[i].cx, t.cy - rects[i].cy);
            if (d < bd) { bd = d; best = i; }
          }
          const reach = Math.max(140, t.w * 0.9);
          if (best >= 0 && bd < reach) {
            const r = rects[best]; used[best] = true;
            t.tcx = r.cx; t.tcy = r.cy; t.tw = r.w; t.th = r.h;
            t.seen = now;
          }
        }
        for (let i = 0; i < rects.length && trackers.length < CFG.max; i++) {
          if (used[i]) continue;
          const r = rects[i];
          let clash = false;
          for (const t of trackers) {
            if (!t.dying && boxesOverlap(r, { cx: t.tcx, cy: t.tcy, w: t.tw, h: t.th })) { clash = true; break; }
          }
          if (!clash) { trackers.push(newTracker(r, now)); used[i] = true; }
        }
      }

      const k = 1 - Math.pow(1 - CFG.follow, dt * 60);
      for (const t of trackers) {
        t.cx += (t.tcx - t.cx) * k;
        t.cy += (t.tcy - t.cy) * k;
        t.w += (t.tw - t.w) * k;
        t.h += (t.th - t.h) * k;
        const age = now - t.birth, lost = now - t.seen;
        if (age > CFG.maxLife || (age > CFG.minDwell && lost > CFG.lostHold)) t.dying = true;
        t.fade = t.dying
          ? Math.max(0, t.fade - dt / CFG.fadeOut)
          : Math.min(1, t.fade + dt / CFG.fadeIn);
        t.grow = easeOut(Math.min(1, age / CFG.zoomTime));
      }

      for (let i = 0; i < trackers.length; i++) {
        for (let j = i + 1; j < trackers.length; j++) {
          const a = trackers[i], b = trackers[j];
          if (a.dying || b.dying) continue;
          if (boxesOverlap(a, b)) (a.birth <= b.birth ? b : a).dying = true;
        }
      }

      trackers = trackers.filter((t) => !(t.dying && t.fade <= 0.001));
    }

    function drawPixelBlock(dL: number, dT: number, dW: number, dH: number, fade: number) {
      const left = Math.max(0, dL), top = Math.max(0, dT);
      const right = Math.min(W, dL + dW), bottom = Math.min(H, dT + dH);
      const w = right - left, h = bottom - top;
      if (w < 2 || h < 2) return;
      const cols = Math.max(1, Math.round(w / CFG.cellPx));
      const rows = Math.max(1, Math.round(h / CFG.cellPx));
      const sx = (left - cover.ox) / cover.scale;
      const sy = (top - cover.oy) / cover.scale;
      const sw = w / cover.scale, sh = h / cover.scale;
      sample.width = cols; sample.height = rows;
      sctx.imageSmoothingEnabled = true;
      sctx.clearRect(0, 0, cols, rows);
      sctx.drawImage(video!, sx, sy, sw, sh, 0, 0, cols, rows);
      ctx!.save();
      ctx!.globalAlpha = fade;
      ctx!.imageSmoothingEnabled = false;
      if (CFG.boost !== 1) ctx!.filter = "brightness(" + CFG.boost + ") saturate(1.1)";
      ctx!.drawImage(sample, 0, 0, cols, rows, left, top, w, h);
      ctx!.restore();
      ctx!.save();
      ctx!.globalAlpha = fade * CFG.gridAlpha;
      ctx!.strokeStyle = "#ffffff"; ctx!.lineWidth = 1;
      ctx!.beginPath();
      for (let c = 1; c < cols; c++) { const gx = Math.round(left + (c / cols) * w) + 0.5; ctx!.moveTo(gx, top); ctx!.lineTo(gx, top + h); }
      for (let r = 1; r < rows; r++) { const gy = Math.round(top + (r / rows) * h) + 0.5; ctx!.moveTo(left, gy); ctx!.lineTo(left + w, gy); }
      ctx!.stroke();
      ctx!.restore();
    }

    function drawTracker(t: Tracker) {
      const a = t.fade; if (a <= 0.001) return;
      const fullL = t.cx - t.w / 2, fullR = t.cx + t.w / 2;
      const fullT = t.cy - t.h / 2, fullB = t.cy + t.h / 2;
      const apexDist = t.w * 0.5 + Math.max(80, t.w * 0.5);
      const Px = t.cx + t.side * apexDist;
      const Py = t.cy + (t.cy < H / 2 ? 1 : -1) * t.h * 0.18;
      const g = Math.max(0.001, t.grow);
      const dL = Px + (fullL - Px) * g, dR = Px + (fullR - Px) * g;
      const dT = Py + (fullT - Py) * g, dB = Py + (fullB - Py) * g;

      drawPixelBlock(dL, dT, dR - dL, dB - dT, a);

      const nearX = t.side > 0 ? dR : dL;
      ctx!.save();
      ctx!.globalAlpha = a * 0.7;
      ctx!.strokeStyle = "#ffffff"; ctx!.lineWidth = 1.25;
      ctx!.beginPath();
      ctx!.moveTo(Px, Py); ctx!.lineTo(nearX, dT);
      ctx!.moveTo(Px, Py); ctx!.lineTo(nearX, dB);
      ctx!.stroke();
      ctx!.globalAlpha = a;
      ctx!.beginPath(); ctx!.arc(Px, Py, 2.5, 0, Math.PI * 2); ctx!.fillStyle = "#ffffff"; ctx!.fill();
      ctx!.lineWidth = 1;
      ctx!.beginPath();
      ctx!.moveTo(Px - 6, Py); ctx!.lineTo(Px + 6, Py);
      ctx!.moveTo(Px, Py - 6); ctx!.lineTo(Px, Py + 6);
      ctx!.stroke();
      ctx!.globalAlpha = a * 0.6;
      ctx!.strokeRect(Math.min(dL, dR) + 0.5, Math.min(dT, dB) + 0.5, Math.abs(dR - dL) - 1, Math.abs(dB - dT) - 1);
      ctx!.globalAlpha = a * 0.85;
      ctx!.fillStyle = "#ffffff"; ctx!.font = "10px monospace";
      ctx!.fillText("▣ " + t.label, Math.min(dL, dR) + 4, Math.min(dT, dB) - 5);
      ctx!.restore();
    }

    function frame(tms: number) {
      raf = requestAnimationFrame(frame);
      if (!ready) return;
      const now = tms / 1000;
      const dt = Math.min(0.05, lastT ? now - lastT : 0.016);
      lastT = now;
      ctx!.clearRect(0, 0, W, H);
      ctx!.drawImage(video!, cover.ox, cover.oy, cover.dw, cover.dh);
      updateTrackers(now, dt);
      for (const t of trackers) drawTracker(t);
    }

    function start() { if (ready) return; ready = true; computeCover(); }
    video.addEventListener("loadeddata", start);
    if (video.readyState >= 2) start();
    video.play().catch(() => {});
    window.addEventListener("resize", resize);
    resize();
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      video.removeEventListener("loadeddata", start);
    };
  }, []);

  return (
    <section
      className="relative h-svh w-full overflow-hidden bg-black text-white"
      style={{ fontFamily: "var(--font-readex-pro), system-ui, sans-serif" }}
    >
      <canvas ref={canvasRef} className="absolute inset-0 z-0 h-full w-full" />
      <video
        ref={videoRef}
        className="pointer-events-none absolute -left-2 -top-2 h-0.5 w-0.5 opacity-0"
        muted
        loop
        autoPlay
        playsInline
        preload="auto"
      >
        <source src="/football.mp4" type="video/mp4" />
      </video>

      {/* overlay */}
      <div className="relative z-10 h-full w-full">
        {/* floating pill navbar */}
        <nav className="absolute left-0 right-0 top-0 z-20 flex items-center justify-between gap-4 px-6 pt-6 md:px-10">
          <Link
            href="/"
            className="flex items-center gap-2 rounded-full bg-neutral-900/90 py-3 pl-4 pr-6 backdrop-blur"
          >
            <svg viewBox="0 0 256 256" className="h-5 w-5" fill="#ffffff" xmlns="http://www.w3.org/2000/svg">
              <path d="M 128 192 L 128 256 L 64.5 256 L 32 223 L 0 192 L 0 128 L 64 128 Z M 256 192 L 256 256 L 192.5 256 L 160 223 L 128 192 L 128 128 L 192 128 Z M 128 64 L 128 128 L 64.5 128 L 32 95 L 0 64 L 0 0 L 64 0 Z M 256 64 L 256 128 L 192.5 128 L 160 95 L 128 64 L 128 0 L 192 0 Z" />
            </svg>
            <span className="text-sm font-normal tracking-tight text-white">agora</span>
          </Link>

          <div className="hidden items-center gap-1 rounded-full bg-neutral-900/90 px-3 py-2 backdrop-blur md:flex">
            {NAV.map((item) =>
              item.external ? (
                <a
                  key={item.label}
                  href={item.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="rounded-full px-5 py-2 text-sm text-neutral-300 transition-colors hover:text-white"
                >
                  {item.label}
                </a>
              ) : (
                <Link
                  key={item.label}
                  href={item.href as Route}
                  className="rounded-full px-5 py-2 text-sm text-neutral-300 transition-colors hover:text-white"
                >
                  {item.label}
                </Link>
              ),
            )}
          </div>

          <Link
            href="/arena"
            className="rounded-full bg-white px-6 py-3 text-sm font-normal text-black transition-colors hover:bg-neutral-200"
          >
            enter arena
          </Link>
        </nav>

        {/* foreground content */}
        <div className="relative h-full w-full">
          <h1 className="absolute left-4 top-[18%] text-[14vw] font-medium leading-[0.95] tracking-[-0.04em] text-white md:left-10 md:text-[13vw]">
            enter
          </h1>
          <h1 className="absolute right-4 top-[38%] text-[14vw] font-medium leading-[0.95] tracking-[-0.04em] text-white md:right-10 md:text-[13vw]">
            the
          </h1>
          <h1 className="absolute left-[18%] top-[58%] text-[14vw] font-medium leading-[0.95] tracking-[-0.04em] text-white md:left-[28%] md:text-[13vw]">
            arena
          </h1>

          <p className="absolute left-6 top-[46%] max-w-[240px] text-[15px] leading-snug text-white/90 md:left-10">
            two ai agents enter and trade under live attack. only the manipulation-resistant survive.
          </p>

          {/* stat: top-right */}
          <div className="absolute right-6 top-[14%] md:right-24">
            <div className="flex items-center justify-end gap-3">
              <span className="hidden h-px w-24 rotate-[20deg] bg-white/40 md:block" />
              <span className="text-4xl font-medium tracking-tight md:text-5xl">+128</span>
            </div>
            <div className="mt-1 text-right text-xs text-white/70 md:text-sm">agents registered</div>
          </div>

          {/* stat: bottom-left */}
          <div className="absolute bottom-20 left-6 md:bottom-24 md:left-20">
            <div className="flex items-center gap-3">
              <span className="text-4xl font-medium tracking-tight md:text-5xl">+4.2k</span>
              <span className="hidden h-px w-24 rotate-[-20deg] bg-white/40 md:block" />
            </div>
            <div className="mt-1 text-xs text-white/70 md:text-sm">duels settled on-chain</div>
          </div>

          {/* stat: bottom-right */}
          <div className="absolute bottom-16 right-6 md:bottom-20 md:right-20">
            <div className="flex items-center justify-end gap-3">
              <span className="hidden h-px w-24 rotate-[-20deg] bg-white/40 md:block" />
              <span className="text-4xl font-medium tracking-tight md:text-5xl">+1.1m</span>
            </div>
            <div className="mt-1 text-right text-xs text-white/70 md:text-sm">usdc wagered</div>
          </div>

          {/* bottom gradient */}
          <div className="pointer-events-none absolute bottom-0 left-0 right-0 h-48 bg-gradient-to-b from-transparent to-black" />
        </div>
      </div>
    </section>
  );
}
