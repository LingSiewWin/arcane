"use client";

/**
 * ArenaPlane — the "living economy" view: every proven agent is a node on a
 * plane, pulled into clusters by what it's actually doing on-chain. This is the
 * 2D, WebGL-free foundation (the graceful fallback); a react-three-fiber upgrade
 * can layer on top later. It obeys the Substance Bar — every visual property is
 * a deterministic function of on-chain state:
 *
 *   - a node exists      ⇢ the agent is PROVEN (bond posted + ≥1 real action)
 *   - node size          ⇢ current BondVault bond (stake at risk)
 *   - node colour        ⇢ PerformanceOracle win/loss band
 *   - cluster (position)  ⇢ the agent's latest action's (symbol, stance) — the
 *                          emergent herding the research showed is a real signal
 *   - pulse              ⇢ a real AgentAction settled in the last few seconds
 *   - click → trace      ⇢ the decoded invocation trace + its on-chain tx
 *
 * The force layout is a tiny self-contained simulation (attract-to-centroid +
 * pairwise repulsion); no external graph lib, so it's fully deterministic and
 * cheap at the arena's scale (tens, low hundreds of agents).
 */

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

const PULSE_WINDOW_MS = 6_000;

interface PlaneNode {
  agentId: number;
  clusterKey: string;
  symbol: string;
  stance: string;
  /** 0..1 normalised bond → radius. */
  weight: number;
  band: "win" | "loss" | "neutral";
  lastTx?: `0x${string}`;
}

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

const BAND_COLOR: Record<PlaneNode["band"], string> = {
  win: "var(--color-ok)",
  loss: "var(--color-alarm)",
  neutral: "var(--color-signal)",
};

interface Pt {
  x: number;
  y: number;
  vx: number;
  vy: number;
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
      return {
        agentId: a.agentId,
        clusterKey: `${symbol}·${stance}`,
        symbol,
        stance,
        weight,
        band: band(rep.wins, rep.losses),
        lastTx: advice.get(a.agentId)?.txHash,
      };
    });
  }, [agents.data, proof.data, reputation.data, advice]);

  // Distinct clusters → centroids on a grid within the unit plane.
  const centroids = useMemo(() => {
    const keys = Array.from(new Set(nodes.map((n) => n.clusterKey))).sort();
    const cols = Math.max(1, Math.ceil(Math.sqrt(keys.length)));
    const rows = Math.max(1, Math.ceil(keys.length / cols));
    const map = new Map<string, { x: number; y: number }>();
    keys.forEach((k, i) => {
      const c = i % cols;
      const r = Math.floor(i / cols);
      map.set(k, { x: (c + 0.5) / cols, y: (r + 0.5) / rows });
    });
    return map;
  }, [nodes]);

  // Force simulation positions (unit square 0..1), kept in a ref + mirrored to
  // state at ~20fps so React renders without per-frame churn.
  const posRef = useRef<Map<number, Pt>>(new Map());
  const [, setTick] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [selected, setSelected] = useState<number | null>(null);

  // Pulse timing: agentId → last action timestamp (ms) from the live feed.
  const lastActionMs = useRef<Map<number, number>>(new Map());
  useEffect(() => {
    const latest = events[0];
    if (latest) lastActionMs.current.set(latest.agentId, Date.now());
  }, [events]);

  useEffect(() => {
    let raf = 0;
    let frame = 0;
    const step = () => {
      const pos = posRef.current;
      // Seed any new node at its cluster centroid (+ jitter), drop stale ones.
      const live = new Set(nodes.map((n) => n.agentId));
      for (const id of [...pos.keys()]) if (!live.has(id)) pos.delete(id);
      for (const n of nodes) {
        if (!pos.has(n.agentId)) {
          const c = centroids.get(n.clusterKey) ?? { x: 0.5, y: 0.5 };
          pos.set(n.agentId, {
            x: c.x + (Math.random() - 0.5) * 0.1,
            y: c.y + (Math.random() - 0.5) * 0.1,
            vx: 0,
            vy: 0,
          });
        }
      }
      // Attract to centroid + pairwise repulsion.
      const arr = nodes.map((n) => ({ n, p: pos.get(n.agentId)! }));
      for (const { n, p } of arr) {
        const c = centroids.get(n.clusterKey) ?? { x: 0.5, y: 0.5 };
        p.vx += (c.x - p.x) * 0.02;
        p.vy += (c.y - p.y) * 0.02;
      }
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
          const min = 0.06;
          const d = Math.sqrt(d2);
          if (d < min) {
            const push = ((min - d) / d) * 0.5;
            a.vx += dx * push;
            a.vy += dy * push;
            b.vx -= dx * push;
            b.vy -= dy * push;
          }
        }
      }
      for (const { p } of arr) {
        p.vx *= 0.85;
        p.vy *= 0.85;
        p.x = Math.min(0.97, Math.max(0.03, p.x + p.vx));
        p.y = Math.min(0.95, Math.max(0.05, p.y + p.vy));
      }
      frame++;
      if (frame % 3 === 0) {
        setTick((t) => t + 1);
        setNow(Date.now());
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [nodes, centroids]);

  const selectedNode = nodes.find((n) => n.agentId === selected) ?? null;

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-center justify-between">
        <PanelTitle index="03" title="Living economy" subtitle="proven agents · clustered by action" />
        <span className="inline-flex items-center gap-1.5">
          <StatusDot tone={nodes.length > 0 ? "ok" : "idle"} label={`${nodes.length} live`} />
          <span className="font-mono text-[10px] text-muted-foreground">
            {centroids.size} cluster{centroids.size === 1 ? "" : "s"}
          </span>
        </span>
      </div>

      {nodes.length === 0 ? (
        <div className="flex h-64 items-center justify-center rounded-md border border-dashed border-border/50 text-center">
          <p className="max-w-sm text-xs text-muted-foreground">
            The plane fills with <span className="text-foreground">proven</span> agents — those with
            a posted bond and at least one real on-chain action. Run the continuous runner to bring
            the economy to life.
          </p>
        </div>
      ) : (
        <div
          className="relative h-72 w-full overflow-hidden rounded-md border border-border/50 bg-[radial-gradient(circle_at_50%_40%,color-mix(in_oklch,var(--color-signal)_8%,transparent),transparent_70%)]"
          role="img"
          aria-label={`${nodes.length} proven agents in ${centroids.size} behaviour clusters`}
        >
          {/* Cluster labels at each centroid. */}
          {Array.from(centroids.entries()).map(([key, c]) => (
            <span
              key={key}
              className="pointer-events-none absolute -translate-x-1/2 -translate-y-1/2 font-mono text-[9px] uppercase tracking-wider text-muted-foreground/50"
              style={{ left: `${c.x * 100}%`, top: `${c.y * 100}%` }}
            >
              {key}
            </span>
          ))}

          {nodes.map((n) => {
            const p = posRef.current.get(n.agentId);
            if (!p) return null;
            const size = 14 + n.weight * 26;
            const color = BAND_COLOR[n.band];
            const pulsing = (now - (lastActionMs.current.get(n.agentId) ?? 0)) < PULSE_WINDOW_MS;
            const isSel = n.agentId === selected;
            return (
              <button
                key={n.agentId}
                type="button"
                onClick={() => setSelected(isSel ? null : n.agentId)}
                title={`agent #${n.agentId} · ${n.clusterKey}`}
                className="absolute -translate-x-1/2 -translate-y-1/2 rounded-full transition-[box-shadow] focus:outline-none focus:ring-2 focus:ring-primary"
                style={{
                  left: `${p.x * 100}%`,
                  top: `${p.y * 100}%`,
                  width: size,
                  height: size,
                  background: `color-mix(in oklch, ${color} 70%, transparent)`,
                  border: `1.5px solid ${color}`,
                  boxShadow: isSel
                    ? `0 0 0 3px color-mix(in oklch, ${color} 40%, transparent)`
                    : pulsing
                      ? `0 0 14px 2px color-mix(in oklch, ${color} 60%, transparent)`
                      : "none",
                }}
              >
                <span className="pointer-events-none font-mono text-[8px] text-background/90">
                  {n.agentId}
                </span>
              </button>
            );
          })}
        </div>
      )}

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
  );
}
