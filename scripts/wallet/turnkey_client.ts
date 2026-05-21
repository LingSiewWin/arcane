/**
 * Thin wrapper around `@turnkey/sdk-server`.
 *
 * Spec contract:
 *   - If TURNKEY_API_PUBLIC_KEY / TURNKEY_API_PRIVATE_KEY / TURNKEY_ORG_ID are set,
 *     generate a real Turnkey-backed EOA (key material stays inside the AWS Nitro TEE).
 *   - Otherwise, generate a random local EOA via viem and warn loudly. This path is
 *     ONLY for unit tests + dry-run development.
 *
 * The Turnkey EOA's only on-chain role is to sign x402 payloads (EIP-712 typed data
 * for Circle Gateway). Funds and identity live on the Circle SCA layer above it.
 */

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import type { TurnkeyEoa } from "./types.js";
import { MissingEnvironment } from "./types.js";

/**
 * Lazy-loaded Turnkey SDK. We `import()` dynamically so that test runs in the
 * local fallback path do NOT require `@turnkey/sdk-server` to be installed.
 */
async function loadTurnkeySdk(): Promise<{
  Turnkey: new (opts: {
    apiBaseUrl: string;
    apiPublicKey: string;
    apiPrivateKey: string;
    defaultOrganizationId: string;
  }) => unknown;
}> {
  const mod = (await import("@turnkey/sdk-server")) as unknown as {
    Turnkey: new (opts: {
      apiBaseUrl: string;
      apiPublicKey: string;
      apiPrivateKey: string;
      defaultOrganizationId: string;
    }) => unknown;
  };
  return mod;
}

/** Env-var presence helper. Returns null if any required Turnkey var is missing. */
export function readTurnkeyEnv(): {
  apiPublicKey: string;
  apiPrivateKey: string;
  organizationId: string;
  apiBaseUrl: string;
} | null {
  const apiPublicKey = process.env.TURNKEY_API_PUBLIC_KEY;
  const apiPrivateKey = process.env.TURNKEY_API_PRIVATE_KEY;
  const organizationId = process.env.TURNKEY_ORG_ID;
  if (!apiPublicKey || !apiPrivateKey || !organizationId) {
    return null;
  }
  return {
    apiPublicKey,
    apiPrivateKey,
    organizationId,
    apiBaseUrl: process.env.TURNKEY_API_BASE_URL ?? "https://api.turnkey.com",
  };
}

/**
 * Generate a Turnkey-backed Ethereum EOA. The signing key is created inside the
 * Turnkey TEE — we never see the raw private key. Returns the public address.
 *
 * Errors:
 *   - throws MissingEnvironment if required envs are absent.
 *   - re-throws Turnkey SDK errors unchanged.
 */
async function generateTurnkeyEoa(name: string): Promise<TurnkeyEoa> {
  const env = readTurnkeyEnv();
  if (!env) {
    throw new MissingEnvironment(
      "TURNKEY_API_PUBLIC_KEY / TURNKEY_API_PRIVATE_KEY / TURNKEY_ORG_ID",
    );
  }
  const { Turnkey } = await loadTurnkeySdk();
  // The Turnkey client surface is intentionally typed as `unknown` because the
  // sdk-server library re-exports its own internal types we'd otherwise have to
  // pin to a specific version. We treat the result as an opaque RPC client.
  const client = new Turnkey({
    apiBaseUrl: env.apiBaseUrl,
    apiPublicKey: env.apiPublicKey,
    apiPrivateKey: env.apiPrivateKey,
    defaultOrganizationId: env.organizationId,
  }) as unknown as {
    apiClient(): {
      createWallet(input: {
        walletName: string;
        accounts: Array<{
          curve: "CURVE_SECP256K1";
          pathFormat: "PATH_FORMAT_BIP32";
          path: string;
          addressFormat: "ADDRESS_FORMAT_ETHEREUM";
        }>;
      }): Promise<{
        walletId: string;
        addresses: string[];
      }>;
    };
  };
  const apiClient = client.apiClient();
  const result = await apiClient.createWallet({
    walletName: `agorahack-${name}-${Date.now()}`,
    accounts: [
      {
        curve: "CURVE_SECP256K1",
        pathFormat: "PATH_FORMAT_BIP32",
        path: "m/44'/60'/0'/0/0",
        addressFormat: "ADDRESS_FORMAT_ETHEREUM",
      },
    ],
  });
  const address = result.addresses[0];
  if (!address || !address.startsWith("0x")) {
    throw new Error(
      `Turnkey createWallet returned malformed address: ${String(address)}`,
    );
  }
  return {
    address: address as `0x${string}`,
    turnkeyWalletId: result.walletId,
    backedByTEE: true,
  };
}

/**
 * Local random EOA fallback. Used ONLY when Turnkey env is unset.
 *
 * Emits a loud stderr warning the first time it's called — this is not safe for
 * any real broadcast and exists purely so tests + `--dry-run` runs don't need a
 * Turnkey account.
 */
let warnedAboutLocalFallback = false;

function generateLocalEoa(): TurnkeyEoa {
  if (!warnedAboutLocalFallback) {
    process.stderr.write(
      "\n[WARN] TURNKEY_* env vars unset — generating a LOCAL random EOA.\n" +
        "[WARN] This key is in-memory only and MUST NOT be used for real broadcasts.\n" +
        "[WARN] Set TURNKEY_API_PUBLIC_KEY/TURNKEY_API_PRIVATE_KEY/TURNKEY_ORG_ID to use the TEE.\n\n",
    );
    warnedAboutLocalFallback = true;
  }
  const privateKey = generatePrivateKey();
  const account = privateKeyToAccount(privateKey);
  return {
    address: account.address,
    privateKey,
    backedByTEE: false,
  };
}

/**
 * Public surface: create or fetch an EOA for the given agent name.
 *
 * Behavior:
 *   - If `forceLocal=true`, always use the local random fallback (test convenience).
 *   - If Turnkey env vars are set AND `forceLocal=false`, generate via Turnkey.
 *   - Otherwise, fall back to local random with a loud warning.
 */
export async function createTurnkeyEoa(
  name: string,
  options: { forceLocal?: boolean } = {},
): Promise<TurnkeyEoa> {
  if (options.forceLocal === true) {
    return generateLocalEoa();
  }
  const env = readTurnkeyEnv();
  if (env === null) {
    return generateLocalEoa();
  }
  return generateTurnkeyEoa(name);
}
