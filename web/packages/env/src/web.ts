import { createEnv } from "@t3-oss/env-nextjs";
import { z } from "zod";

/** A 20-byte 0x-prefixed EVM address. */
const address = z
  .string()
  .regex(/^0x[0-9a-fA-F]{40}$/, "must be a 20-byte 0x address");

/**
 * Build-time validated client env for the web app.
 *
 * Every var here is PUBLIC (NEXT_PUBLIC_*) and read-only — browser reads use the
 * public Arc RPC. There is intentionally NO `server` block: the app holds no
 * server-only secret, and the tokenized "canteen" RPC must never be exposed.
 *
 * All vars are optional: when unset the UI renders honest "not configured"
 * states rather than fabricating data. The schemas still reject malformed
 * values at build time instead of letting them fail silently at runtime.
 */
export const env = createEnv({
  emptyStringAsUndefined: true,
  client: {
    NEXT_PUBLIC_AGENT_REGISTRY: address.optional(),
    NEXT_PUBLIC_PERFORMANCE_ORACLE: address.optional(),
    NEXT_PUBLIC_COLOSSEUM: address.optional(),
    NEXT_PUBLIC_RPC_URL: z.string().url().optional(),
  },
  experimental__runtimeEnv: {
    NEXT_PUBLIC_AGENT_REGISTRY: process.env.NEXT_PUBLIC_AGENT_REGISTRY,
    NEXT_PUBLIC_PERFORMANCE_ORACLE: process.env.NEXT_PUBLIC_PERFORMANCE_ORACLE,
    NEXT_PUBLIC_COLOSSEUM: process.env.NEXT_PUBLIC_COLOSSEUM,
    NEXT_PUBLIC_RPC_URL: process.env.NEXT_PUBLIC_RPC_URL,
  },
});
