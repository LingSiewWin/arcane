import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { NextResponse } from "next/server";

import type { RunResponse, RunStep } from "@/lib/run-types";

export const dynamic = "force-dynamic";

/**
 * Walk up from the Next.js cwd to locate scripts/demo_output.jsonl in the repo
 * root. The app runs locally for the demo, so direct fs access is fine.
 */
function findArtifact(): string | null {
  let dir = process.cwd();
  for (let i = 0; i < 8; i += 1) {
    const candidate = join(dir, "scripts", "demo_output.jsonl");
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function isRunStep(value: unknown): value is RunStep {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.step === "number" && typeof v.name === "string" && "ok" in v;
}

export function GET() {
  const path = findArtifact();

  if (!path) {
    const body: RunResponse = {
      ok: false,
      populated: false,
      steps: [],
      message:
        "No run artifact found. Run `bash scripts/go_live.sh` to populate scripts/demo_output.jsonl.",
    };
    return NextResponse.json(body, { status: 200 });
  }

  const raw = readFileSync(path, "utf8");
  const steps: RunStep[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (isRunStep(parsed)) {
        // ensure evidence exists
        const step = parsed as RunStep;
        if (typeof step.evidence !== "object" || step.evidence === null) {
          step.evidence = {};
        }
        steps.push(step);
      }
    } catch {
      // skip malformed lines — never fabricate data
    }
  }

  steps.sort((a, b) => a.step - b.step);

  const populated = steps.length >= 6;
  const body: RunResponse = {
    ok: true,
    populated,
    steps,
    message: populated
      ? undefined
      : "Run artifact has fewer than 6 steps. Run `bash scripts/go_live.sh` to complete a full run.",
  };
  return NextResponse.json(body, { status: 200 });
}
