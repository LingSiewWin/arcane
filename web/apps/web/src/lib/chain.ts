import { createPublicClient, defineChain, http } from "viem";

import { env } from "@web/env/web";

/**
 * Arc Testnet — PUBLIC RPC only.
 * Never use the tokenized "canteen" RPC here: that endpoint carries a secret
 * and must never reach client code. This public endpoint is read-only and safe.
 */
export const ARC_RPC_URL = "https://rpc.testnet.arc.network";
export const ARC_EXPLORER = "https://testnet.arcscan.app";

/**
 * RPC the browser reads from. Defaults to the public Arc endpoint; can be
 * overridden via NEXT_PUBLIC_RPC_URL (e.g. a local anvil at chain 5042002 for
 * full-stack testing). Reads only — never the tokenized canteen RPC.
 */
const RPC_URL = (env.NEXT_PUBLIC_RPC_URL ?? "").trim() || ARC_RPC_URL;

export const arcTestnet = defineChain({
  id: 5042002,
  name: "Arc Testnet",
  nativeCurrency: {
    // Arc uses USDC as the native gas currency, 18 decimals.
    name: "USD Coin",
    symbol: "USDC",
    decimals: 18,
  },
  rpcUrls: {
    default: { http: [RPC_URL] },
  },
  blockExplorers: {
    default: { name: "Arcscan", url: ARC_EXPLORER },
  },
  testnet: true,
});

export const publicClient = createPublicClient({
  chain: arcTestnet,
  transport: http(RPC_URL),
});

/** Build an explorer tx link. */
export function txUrl(hash: string): string {
  return `${ARC_EXPLORER}/tx/${hash}`;
}

/** Build an explorer address link. */
export function addressUrl(address: string): string {
  return `${ARC_EXPLORER}/address/${address}`;
}
