"use client";

import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { useMemo, useState } from "react";
import { keccak256, stringToHex, type Hex } from "viem";
import {
  useAccount,
  useChainId,
  useSwitchChain,
  useWaitForTransactionReceipt,
  useWriteContract,
} from "wagmi";

import { Button } from "@web/ui/components/button";
import { Card } from "@web/ui/components/card";
import { Input } from "@web/ui/components/input";
import { Label } from "@web/ui/components/label";

import { agentRegistryAbi } from "@/lib/abis";
import { REGISTRY, REGISTRY_CONFIGURED } from "@/lib/arena";
import { arcTestnet } from "@/lib/chain";
import { isConfiguredAddress } from "@/lib/constants";
import { shortHash } from "@/lib/format";

import { ConnectWallet } from "@/components/connect-wallet";
import { Mono, PanelTitle, TxLink } from "@/components/panels/primitives";

import { ArenaEmpty } from "./arena-empty";

/**
 * Operator-path commitment: keccak256 of the raw rules TEXT the operator typed.
 * AgentRegistry.register stores this bytes32 verbatim — it does not validate any
 * canonical encoding. Note this is intentionally NOT the SDK's structured hash
 * (`keccak256(abi.encode(Rule[]))` from ConstitutionRegistry.hashOf); the human
 * operator commits to free-text rules, the SDK commits to compiled Rule structs.
 */
function hashConstitution(rules: string): Hex {
  return keccak256(stringToHex(rules.trim()));
}

export function RegisterForm() {
  const { isConnected } = useAccount();
  const chainId = useChainId();
  const { switchChain, isPending: isSwitching } = useSwitchChain();
  const { writeContract, data: txHash, isPending, error, reset } = useWriteContract();
  const receipt = useWaitForTransactionReceipt({ hash: txHash, chainId: arcTestnet.id });

  const [identityId, setIdentityId] = useState("");
  const [rules, setRules] = useState("");
  const [bondVault, setBondVault] = useState("");
  const [darkPoolUrl, setDarkPoolUrl] = useState("");

  const constitutionHash = useMemo(
    () => (rules.trim() ? hashConstitution(rules) : undefined),
    [rules],
  );

  const wrongNetwork = isConnected && chainId !== arcTestnet.id;
  const idValid = /^\d+$/.test(identityId.trim());
  const vaultValid = isConfiguredAddress(bondVault.trim());
  const canSubmit =
    isConnected &&
    !wrongNetwork &&
    REGISTRY_CONFIGURED &&
    idValid &&
    vaultValid &&
    Boolean(constitutionHash);

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit || !constitutionHash) return;
    // Pin the write to Arc (5042002). wagmi rejects with a wrong-network error
    // if the wallet is elsewhere, rather than silently submitting to the wrong
    // chain. The wrong-network banner above offers a one-click switch.
    writeContract({
      chainId: arcTestnet.id,
      address: REGISTRY,
      abi: agentRegistryAbi,
      functionName: "register",
      args: [
        BigInt(identityId.trim()),
        constitutionHash,
        bondVault.trim() as `0x${string}`,
        darkPoolUrl.trim(),
      ],
    });
  }

  if (!REGISTRY_CONFIGURED) {
    return (
      <ArenaEmpty
        title="Registry not configured"
        cmd="NEXT_PUBLIC_AGENT_REGISTRY=0x… in web/apps/web/.env.local"
      >
        Set the registry address to enable the operator register flow.
      </ArenaEmpty>
    );
  }

  return (
    <Card className="flex flex-col gap-5 p-5">
      <div className="flex items-center justify-between">
        <PanelTitle index="04" title="Register an agent" subtitle="operator wallet flow" />
        <ConnectWallet compact />
      </div>

      {!isConnected ? (
        <ArenaEmpty title="Connect a wallet to register">
          Registration is an on-chain write — it needs your wallet to sign{" "}
          <span className="font-mono">AgentRegistry.register(...)</span> on Arc testnet. Agents
          register via the SDK; this is the human-operator path.
        </ArenaEmpty>
      ) : wrongNetwork ? (
        <div
          role="alert"
          className="flex flex-col items-start gap-3 rounded-md border border-[--color-alarm]/40 bg-[--color-alarm]/10 px-4 py-3"
        >
          <div className="flex items-center gap-2 text-sm text-[--color-alarm]">
            <AlertTriangle className="size-4 shrink-0" aria-hidden="true" />
            <span>
              Wrong network — your wallet is on chain {chainId}. Registration writes to Arc
              Testnet (chain {arcTestnet.id}).
            </span>
          </div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={isSwitching}
            onClick={() => switchChain({ chainId: arcTestnet.id })}
          >
            {isSwitching ? <Loader2 className="size-4 animate-spin" aria-hidden="true" /> : null}
            {isSwitching ? "Switching…" : `Switch to Arc (${arcTestnet.id})`}
          </Button>
        </div>
      ) : (
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="identityId" className="text-xs">
                ERC-8004 identity id
              </Label>
              <Input
                id="identityId"
                inputMode="numeric"
                placeholder="42"
                value={identityId}
                onChange={(e) => setIdentityId(e.target.value)}
                className="font-mono"
              />
              <span className="text-[10px] text-muted-foreground">
                you must own this identity NFT
              </span>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="bondVault" className="text-xs">
                bond vault address
              </Label>
              <Input
                id="bondVault"
                placeholder="0x…"
                value={bondVault}
                onChange={(e) => setBondVault(e.target.value)}
                className="font-mono"
              />
              <span className="text-[10px] text-muted-foreground">
                must hold a posted bond &gt; 0
              </span>
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="rules" className="text-xs">
              constitution rules
            </Label>
            <textarea
              id="rules"
              rows={3}
              placeholder="e.g. max position 10% NAV; no leverage > 3x; SOL/USD only…"
              value={rules}
              onChange={(e) => setRules(e.target.value)}
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-xs outline-none placeholder:text-muted-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
            />
            <span className="text-[10px] text-muted-foreground">
              hashed client-side → constitutionHash:{" "}
              <Mono className="text-primary/80">
                {constitutionHash ? shortHash(constitutionHash) : "—"}
              </Mono>
            </span>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="darkPoolUrl" className="text-xs">
              dark pool url
            </Label>
            <Input
              id="darkPoolUrl"
              placeholder="https://my-agent.example/pool"
              value={darkPoolUrl}
              onChange={(e) => setDarkPoolUrl(e.target.value)}
              className="font-mono"
            />
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" disabled={!canSubmit || isPending}>
              {isPending ? <Loader2 className="size-4 animate-spin" /> : null}
              {isPending ? "Confirm in wallet…" : "Register on Arc"}
            </Button>
            {txHash ? (
              <span className="inline-flex items-center gap-2 text-xs">
                {receipt.isLoading ? (
                  <Loader2 className="size-3.5 animate-spin text-muted-foreground" />
                ) : receipt.isSuccess ? (
                  <CheckCircle2 className="size-4 text-[--color-ok]" />
                ) : null}
                <TxLink hash={txHash} label={shortHash(txHash)} />
                <span className="text-muted-foreground">
                  {receipt.isLoading ? "mining…" : receipt.isSuccess ? "confirmed" : ""}
                </span>
              </span>
            ) : null}
          </div>

          {error ? (
            <p className="rounded-md border border-[--color-alarm]/40 bg-[--color-alarm]/10 px-3 py-2 text-xs text-[--color-alarm]">
              {error.message.split("\n")[0]}
              <button
                type="button"
                onClick={() => reset()}
                className="ml-2 underline underline-offset-2"
              >
                dismiss
              </button>
            </p>
          ) : null}

          <p className="text-[10px] text-muted-foreground">
            Reverts if you don&apos;t own the identity or the bond vault balance is zero — the
            contract enforces both.
          </p>
        </form>
      )}
    </Card>
  );
}
