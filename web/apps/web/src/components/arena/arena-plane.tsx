"use client";

/**
 * ArenaPlane — the "living economy" view: every proven agent is a pixel-human in
 * a colony that darts around the plane with fast, robotic, pixel-stepped motion.
 * Every visual property is still a deterministic function of on-chain state (the
 * Substance Bar) — only the rendering changed from circular nodes to generative
 * pixel sprites drawn on a canvas:
 *
 *   - a sprite exists    ⇢ the agent is PROVEN (bond posted + ≥1 real action)
 *   - sprite scale       ⇢ current BondVault bond (stake at risk)
 *   - sprite colours     ⇢ deterministic warm identity from the agent address
 *   - cluster (gravity)  ⇢ pulled toward its latest action's (symbol, stance)
 *   - detection box      ⇢ a real AgentAction in the last few seconds; the box
 *                          colour is the PerformanceOracle win/loss band and the
 *                          label carries the agent's real win-rate confidence
 *   - click → trace      ⇢ the decoded invocation trace + its on-chain tx
 *
 * The motion is a tiny self-contained simulation: robotic 8-way darting + a weak
 * pull to the cluster centroid + pairwise repulsion, quantized to the pixel grid.
 * Pixel-human generation lives in the framework-free `lib/pixel-avatar` module.
 */

import { useReducedMotion } from "framer-motion";
import { useEffect, useMemo, useRef, useState } from "react";

import { Card } from "@web/ui/components/card";

import {
  proofFor,
  reputationFor,
  useAgents,
  useArenaProof,
  useLiveFeed,
  useReputation,
  type ArenaAgent,
  type FeedEvent,
} from "@/lib/arena";
import { txUrl } from "@/lib/chain";
import { shortHash } from "@/lib/format";
import { PanelTitle, StatusDot } from "@/components/panels/primitives";
import { avatarPixels, GRID, hashSeed, AGENT_PALETTE, type AvatarPixel } from "@/lib/pixel-avatar";

const PULSE_WINDOW_MS = 6_000;

// --- motion tuning (seeded from the approved prototype; tweak freely) ---
const SPEED = 0.3; // base roam, unit/sec
const CLUSTER_PULL = 0.45; // gravity toward the agent's cluster centroid
const TURN_MIN_MS = 250; // robotic direction snaps
const TURN_MAX_MS = 1100;
const TRAIL_LEN = 5;
const REPULSION_MIN = 0.05; // min unit separation before pushing apart
const MARGIN = 0.04; // keep sprites off the edges (unit)

const DIRS: ReadonlyArray<readonly [number, number]> = [
  [1, 0],
  [-1, 0],
  [0, 1],
  [0, -1],
  [1, 1],
  [1, -1],
  [-1, 1],
  [-1, -1],
];

interface PlaneNode {
  agentId: number;
  /** stable string seeding the agent's pixel identity (its address). */
  seed: string;
  clusterKey: string;
  symbol: string;
  stance: string;
  /** 0..1 normalised bond → sprite scale. */
  weight: number;
  band: "win" | "loss" | "neutral";
  /** real win-rate (wins / total) when known, else bond-derived fallback. */
  conf: number;
  lastTx?: `0x${string}`;
}

interface Particle {
  x: number; // unit 0..1
  y: number;
  dx: number; // 8-way direction
  dy: number;
  nextTurn: number; // ms until the next robotic direction snap
  trail: Array<[number, number]>; // recent unit positions, newest first
}

const BAND_VAR: Record<PlaneNode["band"], string> = {
  win: "--color-ok",
  loss: "--color-alarm",
  neutral: "--color-signal",
};

/** Latest advice trace per agent from the recent feed (recency-based herding). */
function latestAdvice(events: FeedEvent[]): Map<number, FeedEvent> {
  const m = new Map<number, FeedEvent>();
  for (const e of events) {
    if (e.kind !== 0 || !e.trace.advice) continue;
    const prev = m.get(e.agentId);
    if (!prev || e.blockNumber > prev.blockNumber) m.set(e.agentId, e);
  }
  return m;
}

function band(wins: number, losses: number): PlaneNode["band"] {
  if (wins === 0 && losses === 0) return "neutral";
  return wins >= losses ? "win" : "loss";
}

/** Distinct cluster keys → centroids on a grid within the unit plane. */
function centroidsOf(clusterKeys: string[]): Map<string, { x: number; y: number }> {
  const keys = Array.from(new Set(clusterKeys)).sort();
  const cols = Math.max(1, Math.ceil(Math.sqrt(keys.length)));
  const rows = Math.max(1, Math.ceil(keys.length / cols));
  const map = new Map<string, { x: number; y: number }>();
  keys.forEach((k, i) => {
    map.set(k, { x: ((i % cols) + 0.5) / cols, y: (Math.floor(i / cols) + 0.5) / rows });
  });
  return map;
}

/**
 * Ambient "awaiting agents" preview shown only when no real agent is proven yet.
 * These are NOT on-chain data: no real ids, no txs, no win/loss, no detection
 * boxes, not clickable — purely a living placeholder so the section is never
 * dead. Real proven agents replace it the instant they appear.
 */
const SAMPLE_SEEDS = [
  "arcane-preview-1",
  "arcane-preview-2",
  "arcane-preview-3",
  "arcane-preview-4",
  "arcane-preview-5",
  "arcane-preview-6",
  "arcane-preview-7",
  "arcane-preview-8",
  "arcane-preview-9",
  "arcane-preview-10",
];

/** Blit a pixel-human at integer canvas origin (ox, oy). legPhase animates legs. */
function drawAvatar(
  ctx: CanvasRenderingContext2D,
  ox: number,
  oy: number,
  cell: number,
  pixels: AvatarPixel[],
  alpha: number,
  legPhase: number,
) {
  ctx.globalAlpha = alpha;
  for (const p of pixels) {
    // bottom rows step left/right with the march to read as "walking"
    const dx = p.y >= 13 && p.x % 2 === legPhase ? 1 : 0;
    ctx.fillStyle = AGENT_PALETTE[p.colorIdx];
    ctx.fillRect(ox + (p.x + dx) * cell, oy + p.y * cell, cell, cell);
  }
  ctx.globalAlpha = 1;
}

export function ArenaPlane() {
  const agents = useAgents();
  const proof = useArenaProof(agents.data);
  const reputation = useReputation();
  const events = useLiveFeed();

  const advice = useMemo(() => latestAdvice(events), [events]);

  // Build the proven node set with all visual properties resolved on-chain.
  const nodes = useMemo<PlaneNode[]>(() => {
    const all = agents.data ?? [];
    const proven = proof.data
      ? all.filter((a) => proofFor(proof.data, a.agentId).proven)
      : [];
    const maxBond = proven.reduce((mx, a) => {
      const b = proofFor(proof.data, a.agentId).bondBalance;
      return b > mx ? b : mx;
    }, BigInt(0));
    return proven.map((a: ArenaAgent) => {
      const p = proofFor(proof.data, a.agentId);
      const adv = advice.get(a.agentId)?.trace.advice;
      const symbol = adv?.symbol || "—";
      const stance = adv?.stance || "idle";
      const rep = reputationFor(reputation.data, a.operator);
      const weight =
        maxBond > BigInt(0) ? Number((p.bondBalance * BigInt(1000)) / maxBond) / 1000 : 0.5;
      const total = rep.wins + rep.losses;
      return {
        agentId: a.agentId,
        seed: a.operator ?? `agent-${a.agentId}`,
        clusterKey: `${symbol}·${stance}`,
        symbol,
        stance,
        weight,
        band: band(rep.wins, rep.losses),
        conf: total > 0 ? rep.wins / total : weight,
        lastTx: advice.get(a.agentId)?.txHash,
      };
    });
  }, [agents.data, proof.data, reputation.data, advice]);

  const centroids = useMemo(() => centroidsOf(nodes.map((n) => n.clusterKey)), [nodes]);

  // No proven agents yet → animate the ambient preview instead of a dead panel.
  const ambient = nodes.length === 0;
  const sampleNodes = useMemo<PlaneNode[]>(
    () =>
      SAMPLE_SEEDS.map((seed, i) => ({
        agentId: -1 - i,
        seed,
        clusterKey: `zone-${i % 4}`,
        symbol: "",
        stance: "",
        weight: ((i % 5) / 5) * 0.7 + 0.25,
        band: "neutral",
        conf: 0,
      })),
    [],
  );
  const displayNodes = ambient ? sampleNodes : nodes;
  const displayCentroids = useMemo(
    () => (ambient ? centroidsOf(sampleNodes.map((n) => n.clusterKey)) : centroids),
    [ambient, sampleNodes, centroids],
  );

  const reduced = useReducedMotion();

  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const posRef = useRef<Map<number, Particle>>(new Map());
  const lastActionMs = useRef<Map<number, number>>(new Map());
  const [selected, setSelected] = useState<number | null>(null);
  const selectedRef = useRef<number | null>(null);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  // Mark an agent as "just acted" when a new event arrives → drives detection box.
  useEffect(() => {
    const latest = events[0];
    if (latest) lastActionMs.current.set(latest.agentId, Date.now());
  }, [events]);

  // The render + motion loop. Re-inits on data / motion-pref change; positions in
  // posRef persist across re-runs so surviving agents keep their place.
  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Resolve theme band colours once (canvas can't read CSS vars at draw time).
    const root = getComputedStyle(document.documentElement);
    const bandColor: Record<PlaneNode["band"], string> = {
      win: root.getPropertyValue(BAND_VAR.win).trim() || "#3ddc97",
      loss: root.getPropertyValue(BAND_VAR.loss).trim() || "#ff5a5a",
      neutral: root.getPropertyValue(BAND_VAR.neutral).trim() || root.getPropertyValue("--primary").trim() || "#2dd4cf",
    };

    let w = wrap.clientWidth;
    let h = wrap.clientHeight;
    const fit = () => {
      w = wrap.clientWidth;
      h = wrap.clientHeight;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.imageSmoothingEnabled = false;
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(wrap);

    // ~half the previous size — daintier sprites.
    const cellFor = (weight: number) => Math.max(1, Math.round(1 + weight * 1.5));

    let raf = 0;
    let last = performance.now();
    const step = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const pos = posRef.current;

      // Sync the particle set with the live node set.
      const live = new Set(displayNodes.map((n) => n.agentId));
      for (const id of [...pos.keys()]) if (!live.has(id)) pos.delete(id);
      for (const n of displayNodes) {
        if (!pos.has(n.agentId)) {
          const c = displayCentroids.get(n.clusterKey) ?? { x: 0.5, y: 0.5 };
          const d = DIRS[Math.floor(Math.random() * DIRS.length)];
          pos.set(n.agentId, {
            x: c.x + (Math.random() - 0.5) * 0.12,
            y: c.y + (Math.random() - 0.5) * 0.12,
            dx: d[0],
            dy: d[1],
            nextTurn: TURN_MIN_MS + Math.random() * (TURN_MAX_MS - TURN_MIN_MS),
            trail: [],
          });
        }
      }

      const arr = displayNodes.map((n) => ({ n, p: pos.get(n.agentId)! }));

      if (!reduced) {
        // Robotic roam + cluster gravity.
        for (const { n, p } of arr) {
          p.nextTurn -= dt * 1000;
          if (p.nextTurn <= 0) {
            const d = DIRS[Math.floor(Math.random() * DIRS.length)];
            p.dx = d[0];
            p.dy = d[1];
            p.nextTurn = TURN_MIN_MS + Math.random() * (TURN_MAX_MS - TURN_MIN_MS);
          }
          const c = displayCentroids.get(n.clusterKey) ?? { x: 0.5, y: 0.5 };
          const len = Math.hypot(p.dx, p.dy) || 1;
          const vx = (p.dx / len) * SPEED + (c.x - p.x) * CLUSTER_PULL;
          const vy = (p.dy / len) * SPEED + (c.y - p.y) * CLUSTER_PULL;
          p.x += vx * dt;
          p.y += vy * dt;
        }
        // Pairwise repulsion (keep sprites from stacking).
        for (let i = 0; i < arr.length; i++) {
          for (let j = i + 1; j < arr.length; j++) {
            const a = arr[i].p;
            const b = arr[j].p;
            let dx = a.x - b.x;
            let dy = a.y - b.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 1e-6) {
              dx = (Math.random() - 0.5) * 0.01;
              dy = (Math.random() - 0.5) * 0.01;
              d2 = dx * dx + dy * dy;
            }
            const d = Math.sqrt(d2);
            if (d < REPULSION_MIN) {
              const push = ((REPULSION_MIN - d) / d) * 0.5;
              a.x += dx * push;
              a.y += dy * push;
              b.x -= dx * push;
              b.y -= dy * push;
            }
          }
        }
        // Bounce off the edges.
        for (const { p } of arr) {
          if (p.x < MARGIN) {
            p.x = MARGIN;
            p.dx = Math.abs(p.dx);
          }
          if (p.x > 1 - MARGIN) {
            p.x = 1 - MARGIN;
            p.dx = -Math.abs(p.dx);
          }
          if (p.y < MARGIN) {
            p.y = MARGIN;
            p.dy = Math.abs(p.dy);
          }
          if (p.y > 1 - MARGIN) {
            p.y = 1 - MARGIN;
            p.dy = -Math.abs(p.dy);
          }
          p.trail.unshift([p.x, p.y]);
          if (p.trail.length > TRAIL_LEN) p.trail.pop();
        }
      }

      // ---- draw ----
      ctx.clearRect(0, 0, w, h);
      const ms = Date.now();
      ctx.textBaseline = "alphabetic";
      ctx.font = "8px ui-monospace, monospace";

      for (const { n, p } of arr) {
        const pixels = avatarPixels(n.seed);
        const cell = cellFor(n.weight);
        const spriteW = GRID * cell;
        const spriteH = GRID * cell;
        const legPhase = reduced ? 0 : (Math.floor(ms / 90) + hashSeed(n.seed)) % 2;

        const place = (ux: number, uy: number) => {
          const cx = ux * w;
          const cy = uy * h;
          return [
            Math.round((cx - spriteW / 2) / cell) * cell,
            Math.round((cy - spriteH / 2) / cell) * cell,
          ] as const;
        };

        // trail (faded)
        if (!reduced) {
          for (let i = p.trail.length - 1; i >= 1; i--) {
            const [tx, ty] = place(p.trail[i][0], p.trail[i][1]);
            drawAvatar(ctx, tx, ty, cell, pixels, 0.05 * (p.trail.length - i), legPhase);
          }
        }

        const [ox, oy] = place(p.x, p.y);
        drawAvatar(ctx, ox, oy, cell, pixels, 1, legPhase);

        // detection box on recent actors (or the selected agent) — real data only
        const age = ms - (lastActionMs.current.get(n.agentId) ?? -Infinity);
        const pulsing = age < PULSE_WINDOW_MS;
        const isSel = n.agentId === selectedRef.current;
        if (!ambient && (pulsing || isSel)) {
          const remaining = Math.max(0, 1 - age / PULSE_WINDOW_MS);
          const col = bandColor[n.band];
          ctx.globalAlpha = isSel ? 0.95 : 0.25 + remaining * 0.6;
          ctx.strokeStyle = col;
          ctx.lineWidth = 1;
          ctx.strokeRect(ox - 2.5, oy - 2.5, spriteW + 5, spriteH + 5);
          ctx.fillStyle = col;
          ctx.fillText(
            `#${n.agentId} ${Math.round(n.conf * 100)}%`,
            ox - 2,
            oy - 6,
          );
          ctx.globalAlpha = 1;
        }
      }

      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [displayNodes, displayCentroids, reduced, ambient]);

  // Canvas click → nearest sprite within reach → select (mirrors the hidden list).
  const handleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (ambient) return; // preview bots aren't real agents — nothing to select
    const wrap = wrapRef.current;
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    let bestId: number | null = null;
    let bestD = Infinity;
    for (const n of nodes) {
      const p = posRef.current.get(n.agentId);
      if (!p) continue;
      const cx = p.x * rect.width;
      const cy = p.y * rect.height;
      const reach = Math.max(12, (1 + n.weight * 1.5) * GRID * 0.6);
      const d = Math.hypot(cx - mx, cy - my);
      if (d < reach && d < bestD) {
        bestD = d;
        bestId = n.agentId;
      }
    }
    setSelected((cur) => (bestId === null ? cur : bestId === cur ? null : bestId));
  };

  const selectedNode = nodes.find((n) => n.agentId === selected) ?? null;

  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <PanelTitle index="03" title="Living economy" subtitle="proven agents · clustered by action" />
        <span className="inline-flex items-center gap-1.5">
          <StatusDot tone={nodes.length > 0 ? "ok" : "idle"} label={`${nodes.length} live`} />
          <span className="font-mono text-[10px] text-muted-foreground">
            {centroids.size} cluster{centroids.size === 1 ? "" : "s"}
          </span>
        </span>
      </div>
      <Card className="flex flex-col gap-3 p-4">
        <div
          ref={wrapRef}
          className="relative h-72 w-full overflow-hidden rounded-md border border-border/50 bg-[radial-gradient(circle_at_50%_40%,color-mix(in_oklch,var(--color-signal)_8%,transparent),transparent_70%)]"
          role="img"
          aria-label={
            ambient
              ? "Living economy preview — awaiting proven agents"
              : `${nodes.length} proven agents darting in ${centroids.size} behaviour clusters`
          }
        >
          <canvas
            ref={canvasRef}
            onClick={handleClick}
            className={`absolute inset-0 size-full ${ambient ? "" : "cursor-pointer"}`}
          />

          {ambient ? null : (
            <>
              {/* Crisp cluster labels overlaid on the canvas. */}
              {Array.from(centroids.entries()).map(([key, c]) => (
                <span
                  key={key}
                  className="pointer-events-none absolute -translate-x-1/2 -translate-y-1/2 font-mono text-[9px] uppercase tracking-wider text-muted-foreground/40"
                  style={{ left: `${c.x * 100}%`, top: `${c.y * 100}%` }}
                >
                  {key}
                </span>
              ))}

              {/* Keyboard / screen-reader path to every agent's trace. */}
              <ul className="sr-only">
                {nodes.map((n) => (
                  <li key={n.agentId}>
                    <button type="button" onClick={() => setSelected(n.agentId)}>
                      Agent #{n.agentId}, cluster {n.clusterKey}, {n.band}, confidence{" "}
                      {Math.round(n.conf * 100)} percent
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>

        {/* Selected agent's latest trace — the click-through to verifiable detail. */}
        {selectedNode ? (
          <div className="flex flex-col gap-1.5 rounded-md border border-border/50 bg-card/40 px-3 py-2.5">
            <div className="flex items-center justify-between">
              <span className="font-mono text-xs">
                Agent #{selectedNode.agentId} ·{" "}
                <span className="text-[--color-signal]">{selectedNode.clusterKey}</span>
              </span>
              {selectedNode.lastTx ? (
                <a
                  href={txUrl(selectedNode.lastTx)}
                  target="_blank"
                  rel="noreferrer"
                  className="font-mono text-[10px] text-primary/90 hover:underline"
                >
                  {shortHash(selectedNode.lastTx)} ↗
                </a>
              ) : null}
            </div>
            <p className="text-[11px] leading-relaxed text-foreground/80">
              {advice.get(selectedNode.agentId)?.trace.advice?.reasoning ??
                "No recent advice in the live window — the agent is proven but quiet right now."}
            </p>
          </div>
        ) : null}
      </Card>
    </section>
  );
}
