"use client";

import { createConfig, http } from "wagmi";
import { injected } from "wagmi/connectors";

import { ARC_RPC_URL, arcTestnet } from "@/lib/chain";

/**
 * Light wallet stack: wagmi + viem's injected() connector only.
 *
 * Why not RainbowKit: it ships its own theming + a large CSS/dependency
 * surface for a single-chain operator flow. wagmi reuses the viem we already
 * depend on, and `injected()` covers MetaMask / Rabby / any EIP-1193 wallet —
 * which is all the operator register flow needs. Writes go through the user's
 * connected wallet (their key), never ours. Reads still use the public RPC.
 */
export const wagmiConfig = createConfig({
  chains: [arcTestnet],
  connectors: [injected()],
  transports: {
    [arcTestnet.id]: http(ARC_RPC_URL),
  },
  ssr: true,
});

declare module "wagmi" {
  interface Register {
    config: typeof wagmiConfig;
  }
}
