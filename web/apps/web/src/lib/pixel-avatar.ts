/**
 * pixel-avatar — deterministic, code-generated pixel-human silhouettes.
 *
 * Pure and framework-free (no React, no canvas): an agent identifier hashes to a
 * seed, a seeded RNG fills a symmetrical humanoid mask, and each filled cell gets
 * a thermal colour index. Same id ⇒ identical pixel set, every time. This is the
 * shared core for the living-economy colony and the (later) ID-card avatar.
 *
 * Guarantees (see the spec / unit checks):
 *   - deterministic: avatarPixels(id) is stable for a given id
 *   - symmetric:     every left-half pixel has a mirrored right-half pixel
 *   - in-bounds:     every pixel sits on a HUMANOID_MASK cell
 *   - valid colour:  0 <= colorIdx < AGENT_PALETTE.length
 */

export const GRID = 16;

/**
 * Left half (8 columns) of a 16-row humanoid. Mirrored to the right at draw
 * time. 1 = a cell the RNG may fill; 0 = forced empty. Editing this changes the
 * body shape (shoulders, arms, build) for every agent at once.
 */
export const HUMANOID_MASK: ReadonlyArray<ReadonlyArray<0 | 1>> = [
  [0, 0, 0, 0, 0, 0, 0, 0], // 0  top margin
  [0, 0, 0, 0, 0, 1, 1, 1], // 1  top of head
  [0, 0, 0, 0, 1, 1, 1, 1], // 2  head
  [0, 0, 0, 0, 1, 1, 1, 1], // 3  head
  [0, 0, 0, 0, 1, 1, 1, 1], // 4  head
  [0, 0, 0, 0, 0, 1, 1, 1], // 5  chin
  [0, 0, 0, 0, 0, 0, 1, 1], // 6  neck
  [0, 0, 0, 1, 1, 1, 1, 1], // 7  shoulders
  [0, 0, 1, 1, 1, 1, 1, 1], // 8  upper chest / arms
  [0, 1, 1, 1, 1, 1, 1, 1], // 9  mid chest
  [1, 1, 1, 0, 0, 1, 1, 1], // 10 arms separating
  [1, 1, 1, 0, 0, 1, 1, 1], // 11 arms
  [1, 1, 1, 0, 0, 1, 1, 1], // 12 arms
  [1, 1, 0, 0, 0, 1, 1, 1], // 13 lower / legs
  [1, 1, 0, 0, 0, 1, 1, 1], // 14 legs
  [1, 1, 0, 0, 0, 1, 1, 1], // 15 legs
];

/**
 * Agent palette — green × purple (Circle web3 theme). The last entry is a rare
 * "spark" colour used for occasional bright edge pixels.
 */
export const AGENT_PALETTE = [
  "#16a34a", // green 600
  "#7c3aed", // violet 600
  "#22c55e", // green 500
  "#a855f7", // purple 500
  "#4ade80", // green 400
  "#d8b4fe", // light purple spark
] as const;

export interface AvatarPixel {
  x: number;
  y: number;
  colorIdx: number;
}

/** FNV-1a hash of a string to a 32-bit unsigned int. */
export function hashSeed(id: string): number {
  let h = 2166136261;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** mulberry32 seeded PRNG → () => float in [0, 1). */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const cache = new Map<string, AvatarPixel[]>();

/**
 * Deterministic pixel set for an agent id (an address or any stable string).
 * Walks the masked left half, fills cells by a per-agent density, picks a
 * thermal colour (rare cyan spark), and mirrors each pixel to the right half.
 */
export function avatarPixels(id: string): AvatarPixel[] {
  const cached = cache.get(id);
  if (cached) return cached;

  const rnd = mulberry32(hashSeed(id));
  const density = 0.62 + rnd() * 0.22; // 0.62–0.84 fill probability
  const pixels: AvatarPixel[] = [];

  for (let y = 0; y < GRID; y++) {
    for (let x = 0; x < GRID / 2; x++) {
      if (HUMANOID_MASK[y][x] !== 1) continue;
      if (rnd() > density) continue;

      const r = rnd();
      // Spread across the palette; rare spark at the very top of the range.
      const colorIdx = r > 0.94 ? AGENT_PALETTE.length - 1 : Math.floor(r * (AGENT_PALETTE.length - 1));

      pixels.push({ x, y, colorIdx });
      pixels.push({ x: GRID - 1 - x, y, colorIdx }); // mirror
    }
  }

  cache.set(id, pixels);
  return pixels;
}
