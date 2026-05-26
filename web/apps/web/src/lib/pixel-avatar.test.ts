import { describe, it, expect } from "vitest";
import {
  GRID,
  HUMANOID_MASK,
  AGENT_PALETTE,
  hashSeed,
  mulberry32,
  avatarPixels,
  type AvatarPixel,
} from "./pixel-avatar";

// Real-world ids: Ethereum-style addresses plus a non-address agent handle.
const IDS = ["0x9E86dA61", "0x515259ed", "0x50eE55D7", "zhgg_001"];

/** Resolve the mask column for any drawn column, accounting for mirroring. */
function maskColumn(x: number): number {
  return x < GRID / 2 ? x : GRID - 1 - x;
}

/** Stable key for set comparison. */
function key(p: AvatarPixel): string {
  return `${p.x},${p.y},${p.colorIdx}`;
}

describe("pixel-avatar module shape", () => {
  it("exposes the expected constants", () => {
    expect(GRID).toBe(16);
    expect(HUMANOID_MASK).toHaveLength(GRID);
    for (const row of HUMANOID_MASK) {
      expect(row).toHaveLength(GRID / 2);
      for (const cell of row) expect(cell === 0 || cell === 1).toBe(true);
    }
    expect(AGENT_PALETTE.length).toBeGreaterThan(0);
    for (const c of AGENT_PALETTE) expect(typeof c).toBe("string");
  });

  it("hashSeed returns a 32-bit unsigned integer", () => {
    for (const id of IDS) {
      const h = hashSeed(id);
      expect(Number.isInteger(h)).toBe(true);
      expect(h).toBeGreaterThanOrEqual(0);
      expect(h).toBeLessThanOrEqual(0xffffffff);
    }
  });

  it("mulberry32 yields floats in [0, 1)", () => {
    const rnd = mulberry32(hashSeed("0x9E86dA61"));
    for (let i = 0; i < 1000; i++) {
      const v = rnd();
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThan(1);
    }
  });
});

describe("guarantee 1 — determinism", () => {
  it("returns a deep-equal pixel set on repeated calls for the same id", () => {
    for (const id of IDS) {
      const first = avatarPixels(id).map((p) => ({ ...p })); // snapshot copy
      const second = avatarPixels(id);
      expect(second).toEqual(first);
      expect(second.length).toBeGreaterThan(0);
    }
  });

  it("produces differing pixel sets for different ids (generally)", () => {
    const signatures = IDS.map((id) =>
      avatarPixels(id)
        .map(key)
        .sort()
        .join("|"),
    );
    const unique = new Set(signatures);
    // All four sample ids must yield distinct silhouettes.
    expect(unique.size).toBe(IDS.length);
  });
});

describe("guarantee 2 — symmetry", () => {
  it("mirrors every pixel across the vertical axis with matching y and colorIdx", () => {
    for (const id of IDS) {
      const pixels = avatarPixels(id);
      const present = new Set(pixels.map(key));
      for (const p of pixels) {
        const mirrorX = GRID - 1 - p.x;
        const mirror = { x: mirrorX, y: p.y, colorIdx: p.colorIdx };
        expect(present.has(key(mirror))).toBe(true);
      }
    }
  });
});

describe("guarantee 3 — in-bounds", () => {
  it("places every pixel on a HUMANOID_MASK cell equal to 1 (mirror-aware)", () => {
    for (const id of IDS) {
      for (const p of avatarPixels(id)) {
        expect(p.x).toBeGreaterThanOrEqual(0);
        expect(p.x).toBeLessThan(GRID);
        expect(p.y).toBeGreaterThanOrEqual(0);
        expect(p.y).toBeLessThan(GRID);
        const mc = maskColumn(p.x);
        expect(HUMANOID_MASK[p.y][mc]).toBe(1);
      }
    }
  });
});

describe("guarantee 4 — valid colour", () => {
  it("uses only integer colorIdx values inside [0, AGENT_PALETTE.length)", () => {
    for (const id of IDS) {
      for (const p of avatarPixels(id)) {
        expect(Number.isInteger(p.colorIdx)).toBe(true);
        expect(p.colorIdx).toBeGreaterThanOrEqual(0);
        expect(p.colorIdx).toBeLessThan(AGENT_PALETTE.length);
      }
    }
  });
});
