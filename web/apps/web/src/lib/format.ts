/** Display helpers for hashes, addresses and numbers. */

export function shortHash(value: string | undefined, head = 6, tail = 4): string {
  if (!value) return "—";
  if (value.length <= head + tail + 2) return value;
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function fmtUsd(value: number | undefined, digits = 2): string {
  if (value === undefined || Number.isNaN(value)) return "—";
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function fmtNum(value: number | bigint | undefined): string {
  if (value === undefined) return "—";
  return Number(value).toLocaleString("en-US");
}

/** Bond balances are stored with 6-dp USDC scaling in the vault. */
export function fmtBond6(value: bigint | undefined): string {
  if (value === undefined) return "—";
  return (Number(value) / 1e6).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  });
}

export function fmtTime(unixSeconds: bigint | number | undefined): string {
  if (unixSeconds === undefined) return "—";
  const ms = Number(unixSeconds) * 1000;
  if (!Number.isFinite(ms) || ms === 0) return "—";
  return new Date(ms).toISOString().replace("T", " ").replace(".000Z", "Z");
}

/** Relative "12s ago" / "3m ago" / "2h ago" from a unix-seconds timestamp. */
export function relativeTime(unixSeconds: bigint | number | undefined, now = Date.now()): string {
  if (unixSeconds === undefined) return "—";
  const then = Number(unixSeconds) * 1000;
  if (!Number.isFinite(then) || then === 0) return "—";
  const diff = Math.max(0, Math.floor((now - then) / 1000));
  if (diff < 5) return "just now";
  if (diff < 60) return `${diff}s ago`;
  const mins = Math.floor(diff / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

/** win/loss → win-rate percentage string, honest "—" when no resolutions. */
export function fmtWinRate(wins: number, losses: number): string {
  const total = wins + losses;
  if (total === 0) return "—";
  return `${((wins / total) * 100).toFixed(0)}%`;
}
