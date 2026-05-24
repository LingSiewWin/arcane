"use client";

import { Fingerprint } from "lucide-react";
import type { Address } from "viem";

import { Card } from "@web/ui/components/card";
import { Skeleton } from "@web/ui/components/skeleton";

import { addressUrl } from "@/lib/chain";
import { ERC8004_IDENTITY_REGISTRY } from "@/lib/constants";
import { shortHash } from "@/lib/format";
import { useIdentityOwner } from "@/lib/hooks";
import type { RunStep } from "@/lib/run-types";

import { Mono, PanelTitle, Stat, TxLink } from "./primitives";

export function IdentityPanel({ spawn }: { spawn: RunStep | undefined }) {
  const reg = spawn?.evidence.addresses?.IdentityRegistry;
  // Read ownerOf from the registry THIS run actually deployed to — a real
  // go_live deploy is readable on public Arc; a local-fork run's ephemeral
  // address is not (the read degrades to "unavailable", which is honest).
  // We must NOT read the canonical registry here: token ids collide across
  // registries, so canonical-registry token #N is an unrelated identity.
  const runRegistryAddr = reg?.address as Address | undefined;
  const isCanonicalRun =
    runRegistryAddr?.toLowerCase() === ERC8004_IDENTITY_REGISTRY.toLowerCase();
  const identityId = reg?.minted_identity_id ?? spawn?.evidence.addresses?.identity_id;
  const owner = useIdentityOwner(runRegistryAddr, identityId);
  const mintTx = reg?.mint_tx;
  const mintedTo = reg?.minted_to;

  return (
    <Card className="gap-0 p-5">
      <div className="flex items-center gap-2">
        <Fingerprint className="size-4 text-primary" />
        <PanelTitle index="06" title="Identity" subtitle="ERC-8004 token, not an API key" />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-4">
        <Stat label="ERC-8004 token id">
          <span className="font-mono text-lg text-primary">
            {identityId !== undefined ? `#${identityId}` : "—"}
          </span>
        </Stat>
        <Stat label="ownerOf (live)" hint="read from chain via viem">
          {owner.isPending ? (
            <Skeleton className="h-4 w-32" />
          ) : owner.isError ? (
            <span className="text-xs text-muted-foreground">unavailable</span>
          ) : (
            <Mono title={owner.data}>{shortHash(owner.data, 10, 6)}</Mono>
          )}
        </Stat>
        <Stat label="minted to">
          <Mono title={mintedTo}>{shortHash(mintedTo, 10, 6)}</Mono>
        </Stat>
        <Stat
          label="registry"
          hint={isCanonicalRun ? "canonical ERC-8004 on Arc" : "this run's registry"}
        >
          {runRegistryAddr ? (
            <a
              href={addressUrl(runRegistryAddr)}
              target="_blank"
              rel="noreferrer"
              className="font-mono text-xs text-primary/90 underline-offset-4 hover:underline"
            >
              {shortHash(runRegistryAddr, 8, 6)}
            </a>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </Stat>
      </div>

      <div className="mt-4 flex items-center justify-between border-t border-border/50 pt-3">
        <p className="max-w-xs text-xs leading-relaxed text-muted-foreground">
          Identity is an on-chain <span className="text-foreground">ERC-8004 token</span> — ownership
          is verifiable by anyone, revocable, and bound to the agent&apos;s authority.
        </p>
        <TxLink hash={mintTx} label="mint tx" />
      </div>
    </Card>
  );
}
