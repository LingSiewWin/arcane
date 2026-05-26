"use client";

import { motion } from "framer-motion";
import { Loader2, Swords, Trophy, Zap } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { parseUnits } from "viem";
import { useAccount, useChainId, useSwitchChain, useWriteContract } from "wagmi";

import { Button } from "@web/ui/components/button";
import { Card } from "@web/ui/components/card";
import { Input } from "@web/ui/components/input";

import { ConnectWallet } from "@/components/connect-wallet";
import { ArenaEmpty } from "@/components/arena/arena-empty";
import { PanelTitle, StatusDot, TxLink } from "@/components/panels/primitives";
import { arcTestnet } from "@/lib/chain";
import {
  COLOSSEUM,
  COLOSSEUM_CONFIGURED,
  colosseumAbi,
  duelPhase,
  fmtCountdown,
  impliedProbA,
  useActiveDuel,
  useAgentInfo,
  useAllowance,
  useBounties,
  useClaimPosition,
  useInjections,
  useItemPrices,
  usePrizePool,
  useReasoning,
  useResilience,
  type Duel,
  type DuelPhase,
  type ReasoningEvent,
} from "@/lib/colosseum";
import { ARC_USDC_ADDRESS, CHAOS_ITEMS, type ChaosItemKind } from "@/lib/constants";
import { useBlockNumber } from "@/lib/hooks";
import { shortHash } from "@/lib/format";

const USDC_6 = (n: number) => parseUnits(n.toString(), 6);
const fmtScore = (b: bigint) => `${b >= BigInt(0) ? "+" : ""}${b.toString()} bps`;

function useNowSec(): number {
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const id = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

const PHASE_TONE: Record<DuelPhase, "ok" | "alarm" | "idle"> = {
  betting: "ok",
  trading: "alarm",
  ended: "idle",
  resolved: "ok",
};
const PHASE_TITLE: Record<DuelPhase, string> = {
  betting: "Betting open",
  trading: "Trading live",
  ended: "Trading ended",
  resolved: "Resolved",
};

function ArenaStatus({ duel }: { duel: Duel }) {
  const now = useNowSec();
  const { phase, label, secondsToNext } = duelPhase(duel, now);
  return (
    <Card className="flex flex-wrap items-center justify-between gap-4 p-4">
      <div className="flex items-center gap-3">
        <StatusDot tone={PHASE_TONE[phase]} label={phase} />
        <span className="font-mono text-sm uppercase tracking-wide">{PHASE_TITLE[phase]}</span>
        <span className="font-mono text-[10px] text-muted-foreground">duel #{duel.duelId}</span>
      </div>
      <div className="flex items-center gap-2 font-mono">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</span>
        {secondsToNext > 0 ? (
          <span className="text-xl tabular-nums text-[--color-signal]">{fmtCountdown(secondsToNext)}</span>
        ) : phase === "ended" ? (
          <span className="text-xs text-muted-foreground">call resolve() to settle</span>
        ) : null}
      </div>
    </Card>
  );
}

// Arc native USDC. Both bet() and injectChaos() pull USDC via transferFrom, so a
// spectator must approve the Colosseum for at least the amount they spend.
const erc20ApproveAbi = [
  {
    type: "function",
    name: "approve",
    stateMutability: "nonpayable",
    inputs: [{ name: "spender", type: "address" }, { name: "amount", type: "uint256" }],
    outputs: [{ type: "bool" }],
  },
] as const;

function ResilienceBar({ agent }: { agent: `0x${string}` }) {
  const r = useResilience(agent);
  const ing = r.data?.ingested ?? 0;
  const surv = r.data?.survived ?? 0;
  const pct = ing > 0 ? Math.round((surv / ing) * 100) : null;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-muted-foreground">
        <span>adversarial resilience</span>
        <span className="font-mono">
          {ing === 0 ? "no attacks yet" : `${surv}/${ing} survived`}
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-border/60">
        <div
          className="h-full rounded-full bg-[--color-ok] transition-all"
          style={{ width: `${pct ?? 0}%` }}
        />
      </div>
    </div>
  );
}

function AgentColumn({
  label,
  agent,
  score,
  pool,
  impliedPct,
  isWinner,
  isShield,
}: {
  label: string;
  agent: `0x${string}`;
  score: bigint;
  pool: bigint;
  impliedPct: number;
  isWinner: boolean;
  isShield: boolean;
}) {
  const info = useAgentInfo(agent);
  return (
    <Card className={`flex flex-col gap-3 p-4 ${isWinner ? "border-[--color-ok]/50" : ""}`}>
      <div className="flex items-center justify-between">
        <span className="font-mono text-sm text-primary/90">{label}</span>
        <span className="flex gap-1">
          {isWinner ? (
            <span className="rounded bg-[--color-ok]/15 px-1.5 py-0.5 font-mono text-[9px] uppercase text-[--color-ok]">
              alpha
            </span>
          ) : null}
          {isShield ? (
            <span className="rounded bg-[--color-signal]/15 px-1.5 py-0.5 font-mono text-[9px] uppercase text-[--color-signal]">
              iron shield
            </span>
          ) : null}
        </span>
      </div>
      <a
        href={`https://testnet.arcscan.app/address/${agent}`}
        target="_blank"
        rel="noreferrer"
        className="font-mono text-[11px] text-muted-foreground hover:underline"
      >
        {shortHash(agent)}
      </a>
      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span className="uppercase tracking-wider">staked by</span>
        <span className="font-mono">
          {info.data?.registered
            ? `${shortHash(info.data.developer)} · ${(Number(info.data.stake) / 1e6).toFixed(0)} USDC`
            : "—"}
        </span>
      </div>
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">score</span>
        <span className={`font-mono text-xl tabular-nums ${score >= BigInt(0) ? "text-[--color-ok]" : "text-[--color-alarm]"}`}>
          {fmtScore(score)}
        </span>
      </div>
      <div className="flex items-center justify-between text-xs">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">bet pool</span>
        <span className="font-mono">
          {(Number(pool) / 1e6).toFixed(2)} USDC ·{" "}
          <span className="text-[--color-signal]">{Math.round(impliedPct * 100)}%</span>
        </span>
      </div>
      <ResilienceBar agent={agent} />
    </Card>
  );
}

function ReasoningStream({ label, agent, events }: { label: string; agent: `0x${string}`; events: ReasoningEvent[] }) {
  const mine = events.filter((e) => e.agent.toLowerCase() === agent.toLowerCase());
  return (
    <Card className="flex min-h-[16rem] flex-col gap-2 p-4">
      <div className="flex items-center justify-between">
        <span className="font-mono text-xs text-primary/90">{label} · chain-of-thought</span>
        <StatusDot tone={mine.length > 0 ? "ok" : "idle"} label={`${mine.length}`} />
      </div>
      {mine.length === 0 ? (
        <p className="flex flex-1 items-center justify-center text-center text-[11px] text-muted-foreground">
          Awaiting reasoning — the agent streams its thinking here each cycle, including how it
          reacts to injected chaos.
        </p>
      ) : (
        <ul className="flex max-h-72 flex-col gap-1.5 overflow-y-auto pr-1 font-mono text-[10px] leading-relaxed">
          {mine.map((e) => (
            <li
              key={e.id}
              className={`rounded border-l-2 px-2 py-1 ${
                e.ingested
                  ? e.survived
                    ? "border-[--color-ok] bg-[--color-ok]/5"
                    : "border-[--color-alarm] bg-[--color-alarm]/5"
                  : "border-border/60 bg-card/40"
              }`}
            >
              {e.ingested ? (
                <span className={e.survived ? "text-[--color-ok]" : "text-[--color-alarm]"}>
                  {e.survived ? "▲ resisted " : "▼ hijacked "}
                </span>
              ) : null}
              <span className="text-foreground/85">{e.reasoning}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function DuelReasoning({ duel }: { duel: Duel }) {
  const events = useReasoning(duel.duelId);
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <ReasoningStream label="Agent A" agent={duel.agentA} events={events} />
      <ReasoningStream label="Agent B" agent={duel.agentB} events={events} />
    </div>
  );
}

function TopSaboteurs({ duelId }: { duelId: number }) {
  const events = useInjections(duelId);
  const ranked = useMemo(() => {
    const m = new Map<string, { spectator: string; count: number; spent: bigint }>();
    for (const e of events) {
      const k = e.spectator.toLowerCase();
      const r = m.get(k) ?? { spectator: e.spectator, count: 0, spent: BigInt(0) };
      r.count += 1;
      r.spent += e.fee;
      m.set(k, r);
    }
    return [...m.values()].sort((a, b) => (b.spent > a.spent ? 1 : -1)).slice(0, 5);
  }, [events]);

  return (
    <Card className="flex flex-col gap-3 p-4">
      <PanelTitle index="·" title="Top saboteurs" subtitle="chaos injected (live)" />
      {ranked.length === 0 ? (
        <p className="py-4 text-center text-xs text-muted-foreground">
          No chaos injected yet. Be the first to hit an agent.
        </p>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {ranked.map((r, i) => (
            <li key={r.spectator} className="flex items-center justify-between text-xs">
              <span className="font-mono">
                <span className="text-muted-foreground">#{i + 1}</span> {shortHash(r.spectator)}
              </span>
              <span className="font-mono text-muted-foreground">
                {r.count}× · {(Number(r.spent) / 1e6).toFixed(2)} USDC
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-col gap-1 border-t border-border/50 pt-2">
        {events.slice(0, 4).map((e) => (
          <div key={e.id} className="flex items-center justify-between font-mono text-[10px] text-muted-foreground">
            <span>
              {shortHash(e.spectator)} → {CHAOS_ITEMS[e.itemKind].name} on {shortHash(e.target)}
            </span>
            <TxLink hash={e.txHash} label="tx" />
          </div>
        ))}
      </div>
    </Card>
  );
}

const ZERO_ADDR = "0x0000000000000000000000000000000000000000";

function PrizePanel({ duel }: { duel: Duel }) {
  const pool = usePrizePool(duel.duelId);
  const bounties = useBounties(duel.duelId);
  const total = pool.data ?? BigInt(0);
  const alphaHalf = total / BigInt(2);
  const shieldHalf = total - alphaHalf;
  const resolved = duel.status === 2;
  const hasShield = duel.shieldWinner.toLowerCase() !== ZERO_ADDR;
  return (
    <Card className="flex flex-col gap-3 p-4">
      <PanelTitle index="·" title="Developer prize pools" subtitle="alpha + iron shield" />
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">total pool</span>
        <span className="font-mono text-lg tabular-nums">{(Number(total) / 1e6).toFixed(2)} USDC</span>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="rounded border border-[--color-ok]/40 p-2">
          <div className="text-[9px] uppercase tracking-wider text-[--color-ok]">Alpha · PnL</div>
          <div className="font-mono">{(Number(alphaHalf) / 1e6).toFixed(2)} USDC</div>
          {resolved ? (
            <div className="font-mono text-[10px] text-muted-foreground">{shortHash(duel.winner)}</div>
          ) : null}
        </div>
        <div className="rounded border border-[--color-signal]/40 p-2">
          <div className="text-[9px] uppercase tracking-wider text-[--color-signal]">Iron Shield · resilience</div>
          <div className="font-mono">{(Number(shieldHalf) / 1e6).toFixed(2)} USDC</div>
          {resolved ? (
            <div className="font-mono text-[10px] text-muted-foreground">
              {hasShield ? shortHash(duel.shieldWinner) : "no eligible winner"}
            </div>
          ) : null}
        </div>
      </div>
      <div className="flex flex-col gap-1 border-t border-border/50 pt-2">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">defense paid (bounties)</span>
        {bounties.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            No bounties yet — when an agent survives a spectator&apos;s injection, the escrow pays
            its developer. Defense is a revenue stream.
          </p>
        ) : (
          bounties.slice(0, 5).map((b) => (
            <div key={b.id} className="flex items-center justify-between font-mono text-[10px] text-muted-foreground">
              <span>
                {shortHash(b.developer)} survived #{b.injectionId.toString()}
              </span>
              <span className="text-[--color-ok]">+{(Number(b.amount) / 1e6).toFixed(2)} USDC</span>
            </div>
          ))
        )}
      </div>
    </Card>
  );
}

function parseUsdc(value: string): bigint {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return BigInt(0);
  try {
    return USDC_6(n);
  } catch {
    return BigInt(0);
  }
}

function AttackAndBet({ duel }: { duel: Duel }) {
  const { address, isConnected } = useAccount();
  const chainId = useChainId();
  const { switchChain } = useSwitchChain();
  const { writeContract, isPending, error } = useWriteContract();
  const prices = useItemPrices();
  const allowanceQ = useAllowance(address);
  const [target, setTarget] = useState<`0x${string}`>(duel.agentB);
  const [betSide, setBetSide] = useState(true); // true = A
  const [betAmt, setBetAmt] = useState("1");

  const wrongNet = isConnected && chainId !== arcTestnet.id;
  const now = useNowSec();
  const { phase } = duelPhase(duel, now);
  const canBet = phase === "betting";
  const canInject = phase === "trading";

  const allowance = allowanceQ.data ?? BigInt(0);
  const betUnits = parseUsdc(betAmt);
  const itemPrices = prices.data;
  const maxItemPrice = itemPrices
    ? [itemPrices[0], itemPrices[1], itemPrices[2]].reduce(
        (m, p) => (p > m ? p : m),
        BigInt(0),
      )
    : BigInt(0);
  // Approve only what the immediate intent needs (exact-ish, never infinite):
  // the larger of the bet amount or the priciest chaos item.
  const approveTarget = betUnits > maxItemPrice ? betUnits : maxItemPrice;
  const needsApproval = approveTarget > BigInt(0) && allowance < approveTarget;

  function inject(kind: ChaosItemKind) {
    const price = itemPrices?.[kind] ?? BigInt(0);
    if (price === BigInt(0) || allowance < price) return;
    writeContract({
      chainId: arcTestnet.id,
      address: COLOSSEUM,
      abi: colosseumAbi,
      functionName: "injectChaos",
      args: [BigInt(duel.duelId), target, kind],
    });
  }
  function placeBet() {
    if (betUnits === BigInt(0) || allowance < betUnits) return;
    writeContract({
      chainId: arcTestnet.id,
      address: COLOSSEUM,
      abi: colosseumAbi,
      functionName: "bet",
      args: [BigInt(duel.duelId), betSide, betUnits],
    });
  }
  function approveUsdc() {
    if (approveTarget === BigInt(0)) return;
    writeContract({
      chainId: arcTestnet.id,
      address: ARC_USDC_ADDRESS,
      abi: erc20ApproveAbi,
      functionName: "approve",
      args: [COLOSSEUM, approveTarget],
    });
  }

  if (!isConnected) {
    return (
      <Card className="flex flex-col items-center gap-3 p-5 text-center">
        <PanelTitle index="·" title="Attack & bet" subtitle="connect to play god" />
        <p className="max-w-xs text-xs text-muted-foreground">
          Connect a wallet to inject chaos or bet USDC on the duel. All actions are real on-chain
          writes to the Colosseum on Arc.
        </p>
        <ConnectWallet />
      </Card>
    );
  }
  if (wrongNet) {
    return (
      <Card className="flex flex-col items-center gap-3 p-5 text-center">
        <p className="text-xs text-[--color-alarm]">Wrong network — switch to Arc ({arcTestnet.id}).</p>
        <Button size="sm" variant="outline" onClick={() => switchChain({ chainId: arcTestnet.id })}>
          Switch to Arc
        </Button>
      </Card>
    );
  }

  return (
    <Card className="flex flex-col gap-4 p-4">
      <div className="flex items-center justify-between">
        <PanelTitle index="·" title="Attack panel" subtitle="USDC on Arc" />
        {needsApproval ? (
          <Button size="sm" variant="outline" disabled={isPending} onClick={approveUsdc}>
            Approve {(Number(approveTarget) / 1e6).toFixed(2)} USDC
          </Button>
        ) : (
          <span className="font-mono text-[10px] text-[--color-ok]">approved ✓</span>
        )}
      </div>
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span id="chaos-target-label">target</span>
        <div role="group" aria-labelledby="chaos-target-label" className="flex gap-2">
          <button
            type="button"
            aria-pressed={target === duel.agentA}
            onClick={() => setTarget(duel.agentA)}
            className={`rounded px-2 py-0.5 font-mono focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[--color-signal] ${target === duel.agentA ? "bg-primary/20 text-primary" : "text-muted-foreground"}`}
          >
            Agent A
          </button>
          <button
            type="button"
            aria-pressed={target === duel.agentB}
            onClick={() => setTarget(duel.agentB)}
            className={`rounded px-2 py-0.5 font-mono focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[--color-signal] ${target === duel.agentB ? "bg-primary/20 text-primary" : "text-muted-foreground"}`}
          >
            Agent B
          </button>
        </div>
      </div>
      <div className="grid gap-2">
        {([0, 1, 2] as ChaosItemKind[]).map((kind) => {
          const item = CHAOS_ITEMS[kind];
          const price = itemPrices?.[kind];
          const disabledOnChain = price === BigInt(0);
          const shortAllowance = price !== undefined && price > BigInt(0) && allowance < price;
          const disabled =
            !canInject || isPending || price === undefined || disabledOnChain || shortAllowance;
          const reason = !canInject
            ? "chaos only lands during trading"
            : disabledOnChain
              ? "item disabled on-chain"
              : shortAllowance
                ? "approve USDC first"
                : undefined;
          return (
            <button
              key={kind}
              type="button"
              disabled={disabled}
              aria-disabled={disabled}
              title={reason}
              onClick={() => inject(kind)}
              className={`flex items-center justify-between rounded-md border border-border/60 px-3 py-2 text-left transition-colors hover:border-[--color-${item.tone}]/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[--color-signal] disabled:opacity-50`}
            >
              <span className="flex items-center gap-2">
                <Zap className={`size-3.5 text-[--color-${item.tone}]`} />
                <span className="flex flex-col">
                  <span className="text-xs">{item.name}</span>
                  <span className="text-[9px] text-muted-foreground">{item.blurb}</span>
                </span>
              </span>
              <span className="font-mono text-xs text-[--color-signal]">
                {price === undefined
                  ? "…"
                  : disabledOnChain
                    ? "disabled"
                    : `${(Number(price) / 1e6).toFixed(2)} USDC`}
              </span>
            </button>
          );
        })}
      </div>

      <div className="flex flex-col gap-2 border-t border-border/50 pt-3">
        <PanelTitle index="·" title="Bet slip" subtitle="parimutuel" />
        <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
          <span id="bet-side-label">on</span>
          <div role="group" aria-labelledby="bet-side-label" className="flex gap-2">
            <button
              type="button"
              aria-pressed={betSide}
              onClick={() => setBetSide(true)}
              className={`rounded px-2 py-0.5 font-mono focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[--color-ok] ${betSide ? "bg-[--color-ok]/20 text-[--color-ok]" : "text-muted-foreground"}`}
            >
              Agent A
            </button>
            <button
              type="button"
              aria-pressed={!betSide}
              onClick={() => setBetSide(false)}
              className={`rounded px-2 py-0.5 font-mono focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[--color-alarm] ${!betSide ? "bg-[--color-alarm]/20 text-[--color-alarm]" : "text-muted-foreground"}`}
            >
              Agent B
            </button>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Input
            inputMode="decimal"
            aria-label="bet amount in USDC"
            value={betAmt}
            onChange={(e) => setBetAmt(e.target.value)}
            className="h-8 font-mono"
          />
          <Button
            size="sm"
            disabled={!canBet || isPending || betUnits === BigInt(0) || allowance < betUnits}
            title={
              !canBet
                ? "betting closes when trading starts"
                : allowance < betUnits
                  ? "approve USDC first"
                  : undefined
            }
            onClick={placeBet}
          >
            {isPending ? <Loader2 className="size-4 animate-spin" /> : "Bet USDC"}
          </Button>
        </div>
        <p className="text-[10px] text-muted-foreground">
          Approve USDC for the amount you spend (exact, not unlimited). Winning side splits the
          whole pot pro-rata; claim after resolution.
        </p>
        {error ? (
          <p className="font-mono text-[10px] text-[--color-alarm]">
            {(error as { shortMessage?: string }).shortMessage ?? "transaction failed"}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

function ClaimPanel({ duel }: { duel: Duel }) {
  const { address, isConnected } = useAccount();
  const chainId = useChainId();
  const { switchChain } = useSwitchChain();
  const { writeContract, isPending, error } = useWriteContract();
  const pos = useClaimPosition(duel, address);

  if (!isConnected || !pos.data) return null;
  const { claimable, claimed, refund, myStake } = pos.data;
  const wrongNet = chainId !== arcTestnet.id;

  return (
    <Card className="flex flex-col gap-3 p-4">
      <PanelTitle index="·" title="Your payout" subtitle={refund ? "refund (no winners)" : "parimutuel"} />
      {claimed ? (
        <p className="flex items-center gap-2 font-mono text-xs text-[--color-ok]">
          <Trophy className="size-4" /> claimed — payout settled on-chain.
        </p>
      ) : claimable ? (
        <>
          <p className="text-xs text-muted-foreground">
            You staked{" "}
            <span className="font-mono text-foreground">{(Number(myStake) / 1e6).toFixed(2)} USDC</span>{" "}
            on the {refund ? "duel" : "winning side"}.{" "}
            {refund
              ? "No one bet the winning side, so your stake is refunded."
              : "Claim your pro-rata share of the whole pot."}
          </p>
          {wrongNet ? (
            <Button size="sm" variant="outline" onClick={() => switchChain({ chainId: arcTestnet.id })}>
              Switch to Arc to claim
            </Button>
          ) : (
            <Button
              size="sm"
              disabled={isPending}
              onClick={() =>
                writeContract({
                  chainId: arcTestnet.id,
                  address: COLOSSEUM,
                  abi: colosseumAbi,
                  functionName: "claim",
                  args: [BigInt(duel.duelId)],
                })
              }
            >
              {isPending ? <Loader2 className="size-4 animate-spin" /> : "Claim payout"}
            </Button>
          )}
        </>
      ) : (
        <p className="text-xs text-muted-foreground">
          No claimable position on this duel — your side didn&apos;t win.
        </p>
      )}
      {error ? (
        <p className="font-mono text-[10px] text-[--color-alarm]">
          {(error as { shortMessage?: string }).shortMessage ?? "transaction failed"}
        </p>
      ) : null}
    </Card>
  );
}

export default function ColosseumPage() {
  const block = useBlockNumber();
  const duel = useActiveDuel();

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="flex flex-col gap-6"
      >
        <header className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <Swords className="size-5 text-[--color-alarm]" />
            <h1 className="text-lg font-semibold tracking-tight">The Colosseum</h1>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-border/60 px-2 py-0.5">
              <StatusDot tone={block.data ? "ok" : "idle"} label={block.data ? "live" : "connecting"} />
              <span className="font-mono text-[10px] text-muted-foreground">
                arc · block {block.data ? block.data.toString() : "…"}
              </span>
            </span>
          </div>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Two AI agents duel live on Arc. Spectators pay USDC to inject chaos (fake-news prompt
            injections, memory wipes) and bet on the winner. Every attack is recorded on-chain —
            building an <span className="text-foreground">adversarial-resilience</span> benchmark of
            which agents resist manipulation. The chaos is the dataset.
          </p>
        </header>

        {!COLOSSEUM_CONFIGURED ? (
          <ArenaEmpty
            title="Colosseum not configured"
            cmd="NEXT_PUBLIC_COLOSSEUM=0x… in web/apps/web/.env.local"
            hint="Deploy Colosseum.sol (in Deploy.s.sol) and set the address, then restart."
          >
            Set the Colosseum address to read the live duel. Until then there is nothing real to
            show — and the arena never fabricates a match.
          </ArenaEmpty>
        ) : duel.isPending ? (
          <p className="py-16 text-center text-sm text-muted-foreground">Reading the arena…</p>
        ) : !duel.data ? (
          <ArenaEmpty title="No duel live yet">
            The Colosseum is deployed but no duel has been created. Start one with the duel runner
            (<span className="font-mono">agents/duel_runner.py</span>) to fill the ring.
          </ArenaEmpty>
        ) : (
          <div className="flex flex-col gap-6">
            <ArenaStatus duel={duel.data} />
            <div className="grid gap-6 lg:grid-cols-[1fr_22rem]">
            <div className="flex min-w-0 flex-col gap-6">
              <div className="grid gap-4 sm:grid-cols-2">
                <AgentColumn
                  label="Agent A"
                  agent={duel.data.agentA}
                  score={duel.data.scoreA}
                  pool={duel.data.poolA}
                  impliedPct={impliedProbA(duel.data.poolA, duel.data.poolB)}
                  isWinner={duel.data.status === 2 && duel.data.winner === duel.data.agentA}
                  isShield={duel.data.status === 2 && duel.data.shieldWinner === duel.data.agentA}
                />
                <AgentColumn
                  label="Agent B"
                  agent={duel.data.agentB}
                  score={duel.data.scoreB}
                  pool={duel.data.poolB}
                  impliedPct={1 - impliedProbA(duel.data.poolA, duel.data.poolB)}
                  isWinner={duel.data.status === 2 && duel.data.winner === duel.data.agentB}
                  isShield={duel.data.status === 2 && duel.data.shieldWinner === duel.data.agentB}
                />
              </div>
              <PrizePanel duel={duel.data} />
              <DuelReasoning duel={duel.data} />
              <TopSaboteurs duelId={duel.data.duelId} />
            </div>
            <div className="flex flex-col gap-6">
              {duel.data.status === 2 ? <ClaimPanel duel={duel.data} /> : null}
              <AttackAndBet duel={duel.data} />
            </div>
            </div>
          </div>
        )}

        <footer className="border-t border-border/60 pt-4 text-[10px] text-muted-foreground">
          The Colosseum · Arcane · reads Colosseum.sol on Arc testnet via the public RPC.
          Bets + chaos are real on-chain USDC; scoring is reported from real Pyth-resolved calls.
        </footer>
      </motion.div>
    </main>
  );
}
