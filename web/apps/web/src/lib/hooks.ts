"use client";

import { useQuery } from "@tanstack/react-query";
import type { Address, Hex } from "viem";

import {
  bondVaultAbi,
  identityRegistryAbi,
  memoryAnchorAbi,
  pythAbi,
} from "@/lib/abis";
import { publicClient } from "@/lib/chain";
import { PYTH_ADDRESS, SOL_USD_FEED_ID } from "@/lib/constants";
import type { RunResponse } from "@/lib/run-types";

/* ----------------------------- run artifact ----------------------------- */

export function useRun() {
  return useQuery<RunResponse>({
    queryKey: ["run"],
    queryFn: async () => {
      const res = await fetch("/api/run", { cache: "no-store" });
      if (!res.ok) throw new Error(`/api/run -> ${res.status}`);
      return (await res.json()) as RunResponse;
    },
    refetchInterval: 30_000,
  });
}

/* ------------------------------ live block ------------------------------ */

export function useBlockNumber() {
  return useQuery<bigint>({
    queryKey: ["arc-block"],
    queryFn: () => publicClient.getBlockNumber(),
    refetchInterval: 5_000,
  });
}

/* ------------------------------- pyth feed ------------------------------ */

export interface PythPrice {
  /** human float, price * 10^expo */
  value: number;
  raw: bigint;
  expo: number;
  conf: bigint;
  publishTime: bigint;
}

export function usePythSolUsd() {
  return useQuery<PythPrice>({
    queryKey: ["pyth-sol-usd"],
    queryFn: async () => {
      const result = (await publicClient.readContract({
        address: PYTH_ADDRESS,
        abi: pythAbi,
        functionName: "getPriceUnsafe",
        args: [SOL_USD_FEED_ID as Hex],
      })) as {
        price: bigint;
        conf: bigint;
        expo: number;
        publishTime: bigint;
      };
      const expo = Number(result.expo);
      return {
        value: Number(result.price) * 10 ** expo,
        raw: result.price,
        expo,
        conf: result.conf,
        publishTime: result.publishTime,
      };
    },
    refetchInterval: 10_000,
  });
}

/* ---------------------------- erc-8004 owner ---------------------------- */

export function useIdentityOwner(registry: Address | undefined, identityId: number | undefined) {
  return useQuery<Address>({
    queryKey: ["identity-owner", registry, identityId],
    enabled: Boolean(registry) && identityId !== undefined,
    queryFn: () =>
      publicClient.readContract({
        address: registry as Address,
        abi: identityRegistryAbi,
        functionName: "ownerOf",
        args: [BigInt(identityId as number)],
      }) as Promise<Address>,
    refetchInterval: 30_000,
  });
}

/* --------------------------- memory anchor ------------------------------ */

export interface AnchorInfo {
  historyLength: bigint;
  root: Hex;
  ownerAtAnchor: Address;
  timestamp: bigint;
}

export function useMemoryAnchor(anchor: Address | undefined, identityId: number | undefined) {
  return useQuery<AnchorInfo>({
    queryKey: ["memory-anchor", anchor, identityId],
    enabled: Boolean(anchor) && identityId !== undefined,
    queryFn: async () => {
      const id = BigInt(identityId as number);
      const historyLength = (await publicClient.readContract({
        address: anchor as Address,
        abi: memoryAnchorAbi,
        functionName: "historyLength",
        args: [id],
      })) as bigint;

      const [root, ownerAtAnchor, timestamp] = (await publicClient.readContract({
        address: anchor as Address,
        abi: memoryAnchorAbi,
        functionName: "anchorAt",
        args: [id, BigInt(0)],
      })) as [Hex, Address, bigint];

      return { historyLength, root, ownerAtAnchor, timestamp };
    },
    refetchInterval: 30_000,
  });
}

/* ----------------------------- bond vault ------------------------------- */

export function useBondBalance(vault: Address | undefined, agent: Address | undefined) {
  return useQuery<bigint>({
    queryKey: ["bond-balance", vault, agent],
    enabled: Boolean(vault) && Boolean(agent),
    queryFn: () =>
      publicClient.readContract({
        address: vault as Address,
        abi: bondVaultAbi,
        functionName: "balanceOf",
        args: [agent as Address],
      }) as Promise<bigint>,
    refetchInterval: 30_000,
  });
}
