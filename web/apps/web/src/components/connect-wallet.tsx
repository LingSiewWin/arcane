"use client";

import { Wallet } from "lucide-react";
import { useAccount, useConnect, useDisconnect } from "wagmi";

import { Button } from "@web/ui/components/button";

import { shortHash } from "@/lib/format";

/**
 * Minimal connect/disconnect control using wagmi's injected connector.
 * No modal library — one click connects the first injected EIP-1193 wallet.
 */
export function ConnectWallet({ compact = false }: { compact?: boolean }) {
  const { address, isConnected } = useAccount();
  const { connect, connectors, isPending } = useConnect();
  const { disconnect } = useDisconnect();
  const injectedConnector = connectors.find((c) => c.type === "injected") ?? connectors[0];

  if (isConnected && address) {
    return (
      <Button
        variant="outline"
        size={compact ? "sm" : "default"}
        onClick={() => disconnect()}
        className="font-mono text-xs"
        title={address}
      >
        <span className="size-2 rounded-full bg-[--color-ok]" />
        {shortHash(address)}
      </Button>
    );
  }

  return (
    <Button
      variant={compact ? "outline" : "default"}
      size={compact ? "sm" : "default"}
      disabled={isPending || !injectedConnector}
      onClick={() => injectedConnector && connect({ connector: injectedConnector })}
    >
      <Wallet className="size-4" />
      {isPending ? "Connecting…" : "Connect wallet"}
    </Button>
  );
}
